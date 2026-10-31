"""``POST /api/eval/sets/{id}/reanchor``: re-anchor a set's references after a re-index.

Maps each sample's stored reference text onto the item's current chunks
(:mod:`culture_library.core.eval_reanchor`), updates the shas in place (sample ids kept),
bumps the set version once when anything changed, and records a re-anchor note under the
set's ``generator.reanchor``. Needs ``write`` scope for the item's kind; an invisible set is
a 404, an unindexed item a 409 ``not_indexed``, and an item whose re-index is still
pending a 409 ``index_pending``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from culture_library.core import Store, eval_reanchor
from culture_library.core import search as sr
from culture_library.server.auth import Principal, check, current_principal, get_store
from culture_library.server.routes_eval import _flat, _vis, _visible_set

router = APIRouter(tags=["eval"])


def _index_pending(conn, item_id: int) -> bool:
    row = conn.execute("SELECT state FROM index_queue WHERE item_id = %s", (item_id,)).fetchone()
    return row is not None and row[0] == "pending"


@router.post("/api/eval/sets/{set_id}/reanchor")
def reanchor_set(
    set_id: int,
    principal: Annotated[Principal, Depends(current_principal)],
    store: Annotated[Store, Depends(get_store)],
):
    check(principal, "write")
    found = _visible_set(store, principal, set_id)
    if found is None:
        return _flat(404, f"eval set {set_id} not found", "not_found")
    _, item = found
    check(principal, "write", item.kind)
    with store.connect() as conn:
        # Chunks are rebuilt in the background after the bytes change; until that job is
        # done they may describe the old text, so anchoring (and moving the set's sha) waits.
        if _index_pending(conn, item.id):
            return _flat(
                409,
                f"item {item.id} is being re-indexed; re-anchor when it is done",
                "index_pending",
            )
        chunks = sr.item_chunks(conn, item.id, _vis(principal))
        if not chunks:
            return _flat(409, f"item {item.id} has no indexed chunks to anchor to", "not_indexed")
        summary = eval_reanchor.apply_reanchor(conn, set_id, chunks, item.sha256)
    return {"set_id": set_id, "item_id": item.id, **summary}
