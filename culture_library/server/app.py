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

from fastapi import APIRouter, FastAPI

from culture_library import __version__
from culture_library.server import ServerConfig

API_VERSION = "1"
_ROUTES_PREFIX = "routes_"

health_router = APIRouter()


@health_router.get("/api/health", tags=["meta"])
def health() -> dict[str, str]:
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


def create_app(config: ServerConfig | None = None) -> FastAPI:
    """Build the app; ``GET /api/health`` plus every auto-discovered router."""
    app = FastAPI(title="culture-library", version=__version__)
    app.state.config = config or ServerConfig()
    app.include_router(health_router)
    for router in _discover_routers():
        app.include_router(router)
    return app


def serve(config: ServerConfig) -> None:  # pragma: no cover - blocking network server
    import uvicorn

    uvicorn.run(create_app(config), host=config.host, port=config.port)


__all__ = ["API_VERSION", "create_app", "serve"]
