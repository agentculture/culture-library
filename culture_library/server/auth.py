"""Who is calling, and what may they do — the auth contract every route uses.

This module is the shared interface between route tasks and the keys task
(plan task t5). Routes depend on :func:`current_principal` and call
:func:`check`; t5 replaces the bodies (API keys with scopes, Cloudflare Access)
without changing these signatures. Until then every request is refused.

Route usage::

    @router.get("/api/thing")
    def thing(principal: Principal = Depends(current_principal)):
        check(principal, "read")
        ...

Tests replace the caller with ``app.dependency_overrides[current_principal]``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import HTTPException, Request

SCOPES = ("read", "write", "admin")


@dataclass(frozen=True)
class Principal:
    """An authenticated caller.

    ``scopes`` is a subset of :data:`SCOPES`; ``kinds`` limits writes to those
    item kinds (``None`` means any kind); ``private`` lets the caller see
    private items; ``via`` says how it authenticated (``"key"`` or ``"access"``).
    """

    key_id: str | None
    scopes: frozenset[str] = field(default_factory=frozenset)
    kinds: frozenset[str] | None = None
    private: bool = False
    via: str = "key"


def current_principal(request: Request) -> Principal:
    """Resolve the caller from the request. Replaced by plan task t5."""
    raise HTTPException(
        status_code=401,
        detail={"detail": "authentication is not configured yet", "code": "auth_unavailable"},
    )


def check(principal: Principal, scope: str, kind: str | None = None) -> None:
    """Raise 403 unless ``principal`` holds ``scope`` (and may write ``kind``)."""
    if scope not in principal.scopes and "admin" not in principal.scopes:
        raise HTTPException(
            status_code=403, detail={"detail": f"needs the {scope} scope", "code": "forbidden"}
        )
    if kind is not None and principal.kinds is not None and kind not in principal.kinds:
        raise HTTPException(
            status_code=403,
            detail={"detail": f"this key cannot write kind {kind}", "code": "forbidden_kind"},
        )
