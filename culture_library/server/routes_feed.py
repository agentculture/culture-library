"""``GET /api/feed?since=<cursor>[&item_id=N][&subject=item|repodoc]``:
ordered change events (read scope).

``item_id`` keeps only that item's events (served from the ``feed(item_id)`` index). Events
about private items appear to callers with private access and to the key that uploaded them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from culture_library.core import Store
from culture_library.core.feed import DEFAULT_LIMIT, MAX_LIMIT, read_since
from culture_library.server.auth import Principal, check, current_principal, get_store

router = APIRouter()


@router.get("/api/feed", tags=["feed"])
def feed(
    since: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    item_id: int | None = Query(None, ge=1),
    subject: str | None = Query(None, pattern="^(item|repodoc)$"),
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
) -> dict:
    check(principal, "read")
    with store.reader() as conn:
        return read_since(
            conn,
            since,
            limit=limit,
            include_private=principal.private,
            owner=principal.key_id,
            item_id=item_id,
            subject=subject,
        )
