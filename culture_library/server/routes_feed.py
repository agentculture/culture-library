"""``GET /api/feed?since=<cursor>[&item_id=N]``: ordered change events (read scope).

``item_id`` keeps only that item's events (served from the ``feed(item_id)`` index). Events
about private items appear to callers with private access and to the key that uploaded them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from culture_library.core import Store
from culture_library.core.feed import DEFAULT_LIMIT, MAX_LIMIT, read_since
from culture_library.server.auth import Principal, check, current_principal

router = APIRouter()


def _store(request: Request) -> Store:
    store = getattr(request.app.state, "store", None)
    if store is None:
        store = request.app.state.store = Store()
    return store


@router.get("/api/feed", tags=["feed"])
def feed(
    request: Request,
    since: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    item_id: int | None = Query(None, ge=1),
    principal: Principal = Depends(current_principal),
) -> dict:
    check(principal, "read")
    with _store(request).reader() as conn:
        return read_since(
            conn,
            since,
            limit=limit,
            include_private=principal.private,
            owner=principal.key_id,
            item_id=item_id,
        )
