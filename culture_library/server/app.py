"""FastAPI app factory for the culture-library server.

Provenance: the shape of this module is cited (cite-don't-import) from
repo agentculture/xteink, commit ea2f643 (branch feat/issue-1),
path xteink/server/app.py. This copy is owned and modified here.

Router auto-discovery: :func:`create_app` imports every module in this package
named ``routes_*.py`` and includes its module-level ``router`` (an
:class:`fastapi.APIRouter`) if it defines one. A route task therefore only adds
a ``routes_<name>.py`` file; it never edits this module.
"""

from __future__ import annotations

import importlib
import pkgutil

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from culture_library import __version__
from culture_library.server import ServerConfig

API_VERSION = "1"
_ROUTES_PREFIX = "routes_"

health_router = APIRouter()


@health_router.get("/api/health", tags=["meta"])
def health(request: Request):
    """``ok`` when the database answers ``SELECT 1``; 503 when it is unreachable (claim c40)."""
    from culture_library.core.errors import CoreError
    from culture_library.server.auth import get_store

    try:
        with get_store(request).reader() as conn:
            conn.execute("SELECT 1").fetchone()
    except CoreError as exc:
        return JSONResponse(
            status_code=503, content={"status": "error", "detail": f"database: {exc}"}
        )
    return {"status": "ok", "version": __version__, "api_version": API_VERSION}


def _discover_routers() -> list[APIRouter]:
    """Return the ``router`` of every ``routes_*`` module in this package."""
    import culture_library.server as pkg

    routers: list[APIRouter] = []
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        if not info.name.startswith(_ROUTES_PREFIX):
            continue
        module = importlib.import_module(f"{pkg.__name__}.{info.name}")
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            routers.append(router)
    return routers


#: Paths whose responses carry data and must never be stored by a browser or Cloudflare.
_NO_STORE_PREFIXES = ("/api", "/opds", "/dav", "/mcp")
NO_STORE = "no-store, private"
#: The site and its static files: cache-able only privately, revalidated on every load, so a
#: pushed change shows up at once and Cloudflare (which skips ``private``) never serves it stale.
REVALIDATE = "private, no-cache"


def cache_policy(path: str) -> str:
    """The ``Cache-Control`` value for a response to ``path``."""
    return NO_STORE if path.startswith(_NO_STORE_PREFIXES) else REVALIDATE


def create_app(config: ServerConfig | None = None) -> FastAPI:
    """Build the app; ``GET /api/health`` plus every auto-discovered router."""
    app = FastAPI(title="culture-library", version=__version__)
    app.state.config = config or ServerConfig()

    @app.middleware("http")
    async def _no_stale_cache(request, call_next):  # noqa: ANN001, ANN202
        response = await call_next(request)
        response.headers["Cache-Control"] = cache_policy(request.url.path)
        response.headers.setdefault("Vary", "Cookie, Authorization, Cf-Access-Jwt-Assertion")
        return response

    app.include_router(health_router)
    for router in _discover_routers():
        app.include_router(router)
    return app


def serve(config: ServerConfig) -> None:  # pragma: no cover - blocking network server
    import uvicorn

    uvicorn.run(create_app(config), host=config.host, port=config.port)


__all__ = ["API_VERSION", "NO_STORE", "REVALIDATE", "cache_policy", "create_app", "serve"]
