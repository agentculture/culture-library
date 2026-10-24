"""Who is calling, and what may they do: the auth contract every route uses.

Provenance: the credential order and the Access/CSRF handling are cited (cite-don't-import)
from agentculture/xteink @ ea2f643 (branch feat/issue-1), path xteink/server/auth.py. This
copy is owned here. Modified: one :class:`Principal` (scopes, kind allow-list, ``private``)
instead of xteink's ``Operator``/device split; no device keys; scope checks via :func:`check`;
the store and the Access verifier are resolved lazily from ``app.state`` / the environment
(``create_app`` is not edited); JWKS outages answer 503 (pocketsync's ``unavailable``).

Route usage::

    @router.get("/api/thing")
    def thing(principal: Principal = Depends(current_principal)):
        check(principal, "read")
        ...

Tests replace the caller with ``app.dependency_overrides[current_principal]``.

Credential order (:func:`current_principal`):

1. Any ``Authorization`` header decides on its own: ``Bearer clk_...`` naming a live key is
   that key's principal; anything else is a 401. This works on every bind, with or without
   Access configured (decision c44: machine clients use keys on loopback/LAN).
2. Otherwise, only when Access is configured (``CULTURE_LIBRARY_ACCESS_TEAM_DOMAIN`` +
   ``CULTURE_LIBRARY_ACCESS_AUD``, or ``app.state.access``), ``Cf-Access-Jwt-Assertion`` is
   verified (RS256 against the team JWKS, ``iss``, ``aud``, ``exp``/``nbf``). A forged or
   wrong-audience token is a 401 even when it reaches the origin directly. Without Access the
   header is ignored. The ``CF_Authorization`` cookie is never read.
3. An Access-authenticated unsafe request (not GET/HEAD/OPTIONS) must also carry
   ``X-Library-Request`` (403 otherwise): SSO is an ambient browser credential, and a custom
   header cannot be sent cross-site without a CORS preflight this app never grants.

Who sees private items (``Principal.private``): admin keys, keys minted with
``private=True``, and Access-authenticated people (the operator). Access callers hold every
scope. Access service tokens are refused (401): machines use scoped keys (decision c44).
Raw keys and tokens are never echoed back or logged.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from culture_library.core import Store, keys

from .access import (
    ACCESS_HEADER,
    CSRF_HEADER,
    AccessConfig,
    AccessConfigError,
    AccessVerifier,
    VerificationError,
)

SCOPES = keys.SCOPES

log = logging.getLogger("culture_library.server.auth")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_UNSET = object()
_lock = threading.Lock()


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


def _error(status: int, detail: str, code: str, **kw) -> HTTPException:
    return HTTPException(status_code=status, detail={"detail": detail, "code": code}, **kw)


def _unauthorized() -> HTTPException:
    return _error(
        401,
        "invalid or missing credentials",
        "unauthorized",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_store(request: Request) -> Store:
    """The app's :class:`Store` (``app.state.store``), built from the environment on first use."""
    state = request.app.state
    store = getattr(state, "store", None)
    if store is None:
        with _lock:
            store = getattr(state, "store", None)
            if store is None:
                store = state.store = Store()
    return store


def get_access(request: Request) -> AccessVerifier | None:
    """The app's Access verifier (``app.state.access``), or one built from the environment.

    ``None`` means Access is off. A partial env pair raises :class:`AccessConfigError`.
    """
    state = request.app.state
    verifier = getattr(state, "access", _UNSET)
    if verifier is _UNSET:
        with _lock:
            verifier = getattr(state, "access", _UNSET)
            if verifier is _UNSET:
                config = AccessConfig.from_env(os.environ)
                verifier = state.access = config.verifier() if config else None
    return verifier


def _key_principal(request: Request) -> Principal:
    scheme, _, raw = request.headers.get("authorization", "").partition(" ")
    raw = raw.strip()
    if scheme.lower() != "bearer" or not raw:
        raise _unauthorized()
    try:
        key = keys.authenticate(get_store(request), raw)
    except keys.AuthError:
        raise _unauthorized() from None
    return Principal(
        key_id=key.key_id,
        scopes=key.effective_scopes(),
        kinds=frozenset(key.kinds) if key.kinds is not None else None,
        private=key.private,
        via="key",
    )


def _access_principal(request: Request, verifier: AccessVerifier, assertion: str) -> Principal:
    where = f"{request.method} {request.url.path}"
    try:
        ident = verifier.verify(assertion.strip())
    except VerificationError as exc:
        log.warning("access jwt refused (%s) on %s", exc.reason, where)
        if exc.reason == "unavailable":
            raise _error(
                503, "Cloudflare Access keys are unavailable", "access_unavailable"
            ) from None
        raise _unauthorized() from None
    if ident.kind != "sso":
        # Decision (issue #3, decider 0.80): service tokens are shared secrets; machines use
        # scoped API keys on the local address (c44), so only a person authenticates via Access.
        log.warning("access service token %s refused on %s", ident.identity, where)
        raise _unauthorized()
    if request.method not in _SAFE_METHODS and not request.headers.get(CSRF_HEADER):
        log.warning("access request without %s refused on %s", CSRF_HEADER, where)
        raise _error(403, f"{CSRF_HEADER} header required", "csrf")
    log.info("access: %s via %s on %s", ident.identity, ident.kind, where)
    return Principal(key_id=None, scopes=frozenset(SCOPES), kinds=None, private=True, via="access")


def current_principal(request: Request) -> Principal:
    """Resolve the caller: a Bearer key, else (if configured) a Cloudflare Access JWT."""
    if "authorization" in request.headers:
        return _key_principal(request)
    assertion = request.headers.get(ACCESS_HEADER)
    if assertion is None:
        raise _unauthorized()
    try:
        verifier = get_access(request)
    except AccessConfigError as exc:
        log.error("%s", exc)
        raise _error(500, "Cloudflare Access is misconfigured", "access_misconfigured") from None
    if verifier is None:
        raise _unauthorized()
    return _access_principal(request, verifier, assertion)


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


__all__ = ["SCOPES", "Principal", "check", "current_principal", "get_access", "get_store"]
