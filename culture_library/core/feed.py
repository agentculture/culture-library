"""Change feed: an append-only event log in the library's Postgres database (table ``feed``).

Later tasks call :func:`append` with the connection of the transaction that changes an
item, so the change and its event commit (or roll back) together. Readers poll
:func:`read_since` with a saved cursor; it reads only the ``feed`` table (and, for an
``owner``, :mod:`~culture_library.core.uploaders`), never ``items``. ``item_id`` narrows it to
one item through the ``feed(item_id)`` index, so no catalog or log scan is needed.

Repo-document events share the log. The ``subject`` column (``item`` by default, ``repodoc``
for :func:`append_repodoc`) says what an event is about. ``item_id`` stays ``NOT NULL`` and
holds the repo document's id for a ``repodoc`` event, so only ``subject`` tells the two id
spaces apart. :func:`read_since` therefore reads ``item`` events unless asked otherwise: a
client written before repodoc events (culture-rules, Gwen, xteink) never sees a repodoc id in
``item_id``. ``subject="repodoc"`` reads only repo-document events, ``subject="all"`` both.
Uploader ownership applies to items only; a private
repodoc event is shown only to callers with private access.

Cursor order is commit order. The cursor is the identity column ``feed.seq`` (selected
``AS cursor``, so the API is unchanged). An identity value is assigned at INSERT time, not at
commit, so two concurrent writers could commit out of order and a reader paging by
``seq > saved`` would skip the earlier one for good. Every append therefore first takes the
transaction-level advisory lock :data:`APPEND_LOCK_KEY` and only then inserts, so the next
``seq`` is drawn while no other appender is between its own insert and its commit. The lock
is released at commit or rollback.

Throughput cost: appends are serialised across the whole database, and the lock is held until
the *caller's* transaction ends, so a slow transaction that appends early blocks every other
appender for its remaining duration. Append late in the transaction (as the item and repodoc
writers do, after their own rows). The feed is a change log written at human pace, so one
writer at a time is acceptable. Readers never take the lock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import uploaders
from .errors import ValidationError
from .jsonvalues import sorted_keys
from .models import KINDS, VISIBILITIES
from .store import now

if TYPE_CHECKING:  # pragma: no cover
    import psycopg

EVENT_TYPES = ("created", "updated", "archived", "purged", "send_requested")
DEFAULT_LIMIT = 500
MAX_LIMIT = 1000

# pg_advisory_xact_lock key serialising feed appends (see the module docstring).
APPEND_LOCK_KEY = 0x6375_6C74_6665_6564  # "cultfeed"

SUBJECTS = ("item", "repodoc")
ALL = "all"
DEFAULT_SUBJECT = "item"
_REPODOC_KINDS = ("document", "spec", "plan", "devague-record", "design")


def _jsonb(value: Any) -> Any:
    """``value`` wrapped for a jsonb parameter (psycopg is imported lazily, as in the store)."""
    from psycopg.types.json import Jsonb

    return Jsonb(value)


def _lock_appends(conn: psycopg.Connection) -> None:
    """Take the append lock until this transaction ends; call before drawing a ``seq``."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (APPEND_LOCK_KEY,))


def append(conn: psycopg.Connection, event: dict[str, Any]) -> int:
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
    _lock_appends(conn)
    row = conn.execute(
        "INSERT INTO feed (type, item_id, sha256, kind, visibility, at, data)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING seq",
        (
            etype,
            event["item_id"],
            event["sha256"],
            event["kind"],
            visibility,
            now(),
            None if data is None else _jsonb(data),
        ),
    ).fetchone()
    return int(row[0])


def append_repodoc(
    conn: psycopg.Connection,
    doc_id: int,
    kind: str,
    sha256: str,
    data: dict[str, Any],
    *,
    type: str = "updated",  # noqa: A002 - mirrors the event key
    visibility: str = "private",
) -> int:
    """Append a repo-document event (``subject='repodoc'``); return its cursor.

    ``doc_id`` is stored in ``item_id``. ``data`` must carry ``repo`` and ``path`` and
    either ``commit`` or ``working`` (a working-tree sha is recorded as ``commit='working'``
    when neither is given). ``kind`` is checked against the repodoc kinds, not :data:`KINDS`.
    """
    if type not in EVENT_TYPES:
        raise ValidationError(f"event type must be one of {', '.join(EVENT_TYPES)}")
    if kind not in _REPODOC_KINDS:
        raise ValidationError(f"repodoc kind must be one of {', '.join(_REPODOC_KINDS)}")
    if visibility not in VISIBILITIES:
        raise ValidationError(f"visibility must be one of {', '.join(VISIBILITIES)}")
    if not data.get("repo") or not data.get("path"):
        raise ValidationError("repodoc event data needs repo and path")
    payload = {
        "repo": data["repo"],
        "path": data["path"],
        "commit": data.get("commit") or "working",
    }
    _lock_appends(conn)
    row = conn.execute(
        "INSERT INTO feed (type, item_id, sha256, kind, visibility, at, data, subject)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, 'repodoc') RETURNING seq",
        (type, int(doc_id), sha256, kind, visibility, now(), _jsonb(payload)),
    ).fetchone()
    return int(row[0])


def read_since(
    conn: psycopg.Connection,
    since: int = 0,
    *,
    limit: int = DEFAULT_LIMIT,
    include_private: bool = True,
    owner: str | None = None,
    item_id: int | None = None,
    subject: str | None = None,
) -> dict[str, Any]:
    """Events with ``cursor > since`` in order: ``{events, cursor, more}``.

    ``limit`` bounds the raw rows scanned. With ``include_private=False`` events about
    private items are omitted unless ``owner`` (a key id) uploaded the item, but ``cursor``
    still advances past them. ``item_id`` keeps only that item's events (indexed; it implies
    ``subject='item'``). ``subject`` is ``item`` (the default, also for ``None``),
    ``repodoc``, or ``all`` for both interleaved in cursor order.
    """
    subject = DEFAULT_SUBJECT if subject is None else subject
    if subject != ALL and subject not in SUBJECTS:
        raise ValidationError(f"subject must be one of {', '.join((*SUBJECTS, ALL))}")
    limit = max(1, min(int(limit), MAX_LIMIT))
    vis, vis_params = uploaders.visible_sql(include_private, owner, item_id="item_id")
    if include_private:
        shown = "1=1"
    else:
        shown = f"(subject = 'item' AND {vis}) OR (subject = 'repodoc' AND visibility = 'public')"
    where, params = "seq > %s", [since]
    if item_id is not None:
        where, params = "subject = 'item' AND item_id = %s AND seq > %s", [int(item_id), since]
    elif subject != ALL:
        where, params = "subject = %s AND seq > %s", [subject, since]
    rows = conn.execute(
        "SELECT seq AS cursor, type, item_id, sha256, kind, visibility, at, data, subject,"
        f" ({shown}) AS shown"
        f" FROM feed WHERE {where} ORDER BY seq LIMIT %s",  # nosec B608 - fixed fragments
        (*vis_params, *params, limit + 1),
    ).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    events = []
    for r in rows:
        if not r[9]:
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
                "data": sorted_keys(r[7]),
                "subject": r[8],
            }
        )
    return {"events": events, "cursor": rows[-1][0] if rows else since, "more": more}
