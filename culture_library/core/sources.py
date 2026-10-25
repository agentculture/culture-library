"""Per-item source manifests (table ``item_sources``): where an item's content came from.

One row per item, holding a JSON manifest (for example the URLs a RAG evaluation set may cite).
The row cascades away with its item. Stdlib only, no network.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .store import now, register_schema

register_schema("""
CREATE TABLE IF NOT EXISTS item_sources (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    manifest_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
""")


def put(conn: sqlite3.Connection, item_id: int, manifest: Any) -> None:
    """Set (replace) the item's manifest on the caller's transaction."""
    conn.execute(
        "INSERT INTO item_sources (item_id, manifest_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(item_id) DO UPDATE SET manifest_json = excluded.manifest_json, "
        "updated_at = excluded.updated_at",
        (item_id, json.dumps(manifest, sort_keys=True), now()),
    )


def get(conn: sqlite3.Connection, item_id: int) -> Any | None:
    """The item's manifest, or ``None`` when it has none."""
    row = conn.execute(
        "SELECT manifest_json FROM item_sources WHERE item_id = ?", (item_id,)
    ).fetchone()
    return None if row is None else json.loads(row[0])


def get_with_meta(conn: sqlite3.Connection, item_id: int) -> tuple[Any, str] | None:
    """``(manifest, updated_at)`` for the item, or ``None`` when it has none."""
    row = conn.execute(
        "SELECT manifest_json, updated_at FROM item_sources WHERE item_id = ?", (item_id,)
    ).fetchone()
    return None if row is None else (json.loads(row[0]), row[1])


__all__ = ["get", "get_with_meta", "put"]
