"""Change feed: an append-only event log in the same SQLite DB (table ``feed``).

Later tasks call :func:`append` with the connection of the transaction that changes an
item, so the change and its event commit (or roll back) together. Readers poll
:func:`read_since` with a saved cursor; it reads only the ``feed`` table, never ``items``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .errors import ValidationError
from .models import KINDS, VISIBILITIES
from .store import now, register_schema

EVENT_TYPES = ("created", "updated", "archived", "purged", "send_requested")
DEFAULT_LIMIT = 500
MAX_LIMIT = 1000

register_schema("""
CREATE TABLE IF NOT EXISTS feed (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    kind TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'private',
    at TEXT NOT NULL,
    data TEXT
)
""")


def append(conn: sqlite3.Connection, event: dict[str, Any]) -> int:
    """Append ``event`` on the caller's connection/transaction; return its cursor.

    Required keys: ``type`` (one of :data:`EVENT_TYPES`), ``item_id``, ``sha256``,
    ``kind``. Optional: ``visibility`` (default ``private``), ``data`` (JSON-able dict).
    """
    etype = event.get("type")
    if etype not in EVENT_TYPES:
        raise ValidationError(f"event type must be one of {', '.join(EVENT_TYPES)}")
    for key in ("item_id", "sha256", "kind"):
        if event.get(key) is None:
            raise ValidationError(f"event needs {key}")
    if event["kind"] not in KINDS:
        raise ValidationError(f"kind must be one of {', '.join(KINDS)}")
    visibility = event.get("visibility", "private")
    if visibility not in VISIBILITIES:
        raise ValidationError(f"visibility must be one of {', '.join(VISIBILITIES)}")
    data = event.get("data")
    cur = conn.execute(
        "INSERT INTO feed (type, item_id, sha256, kind, visibility, at, data)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            etype,
            event["item_id"],
            event["sha256"],
            event["kind"],
            visibility,
            now(),
            None if data is None else json.dumps(data, sort_keys=True),
        ),
    )
    return int(cur.lastrowid)


def read_since(
    conn: sqlite3.Connection,
    since: int = 0,
    *,
    limit: int = DEFAULT_LIMIT,
    include_private: bool = True,
) -> dict[str, Any]:
    """Events with ``cursor > since`` in order: ``{events, cursor, more}``.

    ``limit`` bounds the raw rows scanned. With ``include_private=False`` events about
    private items are omitted, but ``cursor`` still advances past them.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    rows = conn.execute(
        "SELECT cursor, type, item_id, sha256, kind, visibility, at, data FROM feed"
        " WHERE cursor > ? ORDER BY cursor LIMIT ?",
        (since, limit + 1),
    ).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    events = []
    for r in rows:
        if not include_private and r[5] != "public":
            continue
        events.append(
            {
                "cursor": r[0],
                "type": r[1],
                "item_id": r[2],
                "sha256": r[3],
                "kind": r[4],
                "visibility": r[5],
                "at": r[6],
                "data": None if r[7] is None else json.loads(r[7]),
            }
        )
    return {"events": events, "cursor": rows[-1][0] if rows else since, "more": more}
