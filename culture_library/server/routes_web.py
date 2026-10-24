"""The library.culture.dev site: ``GET /`` and ``GET /static/<file>``, with security headers.

New module, written for plan task t15. The page is ported from the approved prototype
(``docs/design/prototype.html``; the contract is ``docs/design/library-site.md``): plain ES
modules and one stylesheet under ``culture_library/web/static``, no build step, no framework.

The shell is static and holds no data, so it is served without credentials; every request
for data goes to ``/api/...`` and is authenticated there (Cloudflare Access in production, a
key kept in ``sessionStorage`` for local development).

Every response carries a strict Content-Security-Policy: scripts and styles only from this
origin (Lato is self-hosted under ``/static/fonts``), no inline script or
``style=`` attribute, no plugins, no ``<base>``, no framing. Item text reaches the page
through ``textContent``; the CSP is the second wall if that ever slips.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response

STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"

CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "font-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}
_FONTS = "fonts/"

router = APIRouter(tags=["site"], include_in_schema=False)


def _headers(cache: str) -> dict[str, str]:
    return {**SECURITY_HEADERS, "Cache-Control": cache}


def _static_file(name: str) -> Path | None:
    """The file ``name`` in :data:`STATIC_DIR` or its ``fonts/``, or ``None`` (nothing deeper)."""
    if name.startswith(_FONTS):  # the one subdirectory: the self-hosted Lato files
        base, leaf = STATIC_DIR / "fonts", name[len(_FONTS) :]
    else:
        base, leaf = STATIC_DIR, name
    if not leaf or "/" in leaf or "\\" in leaf or leaf.startswith("."):
        return None
    path = (base / leaf).resolve()
    if path.parent != base.resolve() or not path.is_file():
        return None
    if path.suffix not in _TYPES:
        return None
    return path


def _not_found() -> Response:
    return Response(
        "not found", status_code=404, media_type="text/plain", headers=_headers("no-store")
    )


@router.get("/")
def index() -> Response:
    """The site shell (``index.html``); the modules fetch everything else."""
    return FileResponse(
        STATIC_DIR / "index.html", media_type=_TYPES[".html"], headers=_headers("no-cache")
    )


@router.get("/static/{name:path}")
def static(name: str) -> Response:
    """One file of the site (modules, stylesheet); never anything outside ``web/static``."""
    path = _static_file(name)
    if path is None:
        return _not_found()
    return FileResponse(path, media_type=_TYPES[path.suffix], headers=_headers("no-cache"))
