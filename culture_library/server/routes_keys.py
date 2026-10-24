"""``/api/keys``: list, mint and revoke API keys (admin only).

Provenance: shape cited (cite-don't-import) from agentculture/xteink @ ea2f643 (branch
feat/issue-1), the ``/api/keys`` routes in xteink/server/app.py. Modified: scopes, kinds and
the private flag on mint; admin scope required (xteink allowed any operator); the raw key is
returned once, in the mint response only.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from culture_library.core import NotFoundError, Store, ValidationError, keys

from .auth import Principal, check, current_principal, get_store

router = APIRouter(tags=["keys"])


class MintRequest(BaseModel):
    name: str
    scopes: list[str]
    kinds: list[str] | None = None
    private: bool = False


@router.get("/api/keys")
def list_keys(
    principal: Principal = Depends(current_principal), store: Store = Depends(get_store)
) -> dict:
    check(principal, "admin")
    return {"keys": [k.to_dict() for k in keys.list_keys(store)]}


@router.post("/api/keys", status_code=201)
def mint_key(
    body: MintRequest,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
) -> dict:
    check(principal, "admin")
    try:
        key, raw = keys.mint(store, body.name, body.scopes, kinds=body.kinds, private=body.private)
    except ValidationError as exc:
        raise HTTPException(422, detail={"detail": str(exc), "code": "invalid"}) from None
    return {"api_key": key.to_dict(), "key": raw}


@router.delete("/api/keys/{api_key_id}")
def revoke_key(
    api_key_id: int,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
) -> dict:
    check(principal, "admin")
    try:
        key = keys.revoke(store, api_key_id)
    except NotFoundError as exc:
        raise HTTPException(404, detail={"detail": str(exc), "code": "not_found"}) from None
    return {"api_key": key.to_dict()}
