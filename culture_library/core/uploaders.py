"""Which key uploaded each item (table ``item_uploaders``), and the visibility rule using it.

New module (plan deviation d4, decider 0.74). The uploader used to live only in the feed's
``created`` event data (``{"uploader": key_id}``); :func:`culture_library.core.library.add`
now also records it here, inside the insert transaction, and the library schema backfills it
from existing feed events on open.

The visibility rule every read applies: an item is visible to a caller if it is public, or
the caller has private access, or the caller's key uploaded it. :func:`visible_sql` is that
rule as a SQL condition; :func:`is_visible` is the same rule for one loaded item. Rows
outlive a purge on purpose (item ids are never reused by ordinary inserts), so an uploader
still sees the ``purged`` event of its own private item; :func:`record` replaces any stale
row when an explicit id is inserted again.

Stdlib only, no network.
"""

from __future__ import annotations

import sqlite3

from .store import register_schema

register_schema("""
CREATE TABLE IF NOT EXISTS item_uploaders (
    item_id INTEGER PRIMARY KEY,
    key_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS item_uploaders_key ON item_uploaders(key_id)
""")

_OWNED = "IN (SELECT item_id FROM item_uploaders WHERE key_id = ?)"


def record(conn: sqlite3.Connection, item_id: int, key_id: str | None) -> None:
    """Record (or clear) the uploader of a newly inserted item, on the caller's transaction."""
    conn.execute("DELETE FROM item_uploaders WHERE item_id = ?", (item_id,))
    if key_id:
        conn.execute(
            "INSERT INTO item_uploaders (item_id, key_id) VALUES (?, ?)", (item_id, key_id)
        )


def uploader(conn: sqlite3.Connection, item_id: int) -> str | None:
    row = conn.execute("SELECT key_id FROM item_uploaders WHERE item_id = ?", (item_id,)).fetchone()
    return row[0] if row else None


def uploaded_by(conn: sqlite3.Connection, item_id: int, key_id: str | None) -> bool:
    """True when ``key_id`` is the key whose upload created ``item_id``."""
    return bool(key_id) and uploader(conn, item_id) == key_id


def visible_sql(
    include_private: bool,
    owner: str | None = None,
    *,
    visibility: str = "visibility",
    item_id: str = "id",
) -> tuple[str, list[str]]:
    """``(condition, params)``: public, or private access, or uploaded by ``owner``.

    ``visibility`` and ``item_id`` name the columns (e.g. ``i.visibility``, ``i.id``).
    """
    if include_private:
        return "1=1", []
    if owner:
        return f"({visibility} = 'public' OR {item_id} {_OWNED})", [owner]
    return f"{visibility} = 'public'", []


def is_visible(
    conn: sqlite3.Connection,
    item_id: int,
    visibility: str,
    include_private: bool,
    owner: str | None = None,
) -> bool:
    """The rule for one item: public, private access, or uploaded by ``owner``."""
    return visibility == "public" or include_private or uploaded_by(conn, item_id, owner)


__all__ = ["is_visible", "record", "uploaded_by", "uploader", "visible_sql"]
