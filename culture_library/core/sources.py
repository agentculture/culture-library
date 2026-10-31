"""Per-item source manifests (table ``item_sources``): where an item's content came from.

One row per item, holding a JSON manifest (for example the URLs a RAG evaluation set may cite).
The row cascades away with its item. Stored as ``jsonb`` in Postgres; no network.
"""

from __future__ import annotations

from typing import Any

from .store import now


def put(conn: Any, item_id: int, manifest: Any) -> None:
    """Set (replace) the item's manifest on the caller's transaction."""
    from psycopg.types.json import Jsonb  # the [pg] extra; a Store already needed it

    conn.execute(
        "INSERT INTO item_sources (item_id, manifest_json, updated_at) VALUES (%s, %s, %s) "
        "ON CONFLICT (item_id) DO UPDATE SET manifest_json = excluded.manifest_json, "
        "updated_at = excluded.updated_at",
        (item_id, Jsonb(manifest), now()),
    )


def get(conn: Any, item_id: int) -> Any | None:
    """The item's manifest, or ``None`` when it has none."""
    row = conn.execute(
        "SELECT manifest_json FROM item_sources WHERE item_id = %s", (item_id,)
    ).fetchone()
    return None if row is None else row[0]


def get_with_meta(conn: Any, item_id: int) -> tuple[Any, str] | None:
    """``(manifest, updated_at)`` for the item, or ``None`` when it has none."""
    row = conn.execute(
        "SELECT manifest_json, updated_at FROM item_sources WHERE item_id = %s", (item_id,)
    ).fetchone()
    return None if row is None else (row[0], row[1])


__all__ = ["get", "get_with_meta", "put"]
