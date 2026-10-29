"""``POST /api/eval/sets/{id}/reanchor``: re-anchor a set's references after a re-index.

Maps each sample's stored reference text onto the item's current chunks
(:mod:`culture_library.core.eval_reanchor`), updates the shas in place (sample ids kept),
bumps the set version once when anything changed, and records a re-anchor note under the
set's ``generator.reanchor``. Needs ``write`` scope for the item's kind; an invisible set is
a 404, an unindexed item a 409 ``not_indexed``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from culture_library.core import Store, eval_reanchor
from culture_library.core import search as sr
from culture_library.server.auth import Principal, check, current_principal, get_store
from culture_library.server.routes_eval import _flat, _vis, _visible_set

router = APIRouter(tags=["eval"])


@router.post("/api/eval/sets/{set_id}/reanchor")
def reanchor_set(
    set_id: int,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
):
    check(principal, "write")
    found = _visible_set(store, principal, set_id)
    if found is None:
        return _flat(404, f"eval set {set_id} not found", "not_found")
    row, item = found
    check(principal, "write", item.kind)
    with store.connect() as conn:
        chunks = sr.item_chunks(conn, item.id, _vis(principal))
        if not chunks:
            return _flat(409, f"item {item.id} has no indexed chunks to anchor to", "not_indexed")
        summary = eval_reanchor.apply_reanchor(conn, set_id, chunks)
    return {"set_id": set_id, "item_id": item.id, **summary}
