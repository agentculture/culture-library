"""culture-library HTTP server (requires the ``[server]`` extra).

This package ``__init__`` stays dependency-free so the CLI can import
:class:`ServerConfig` without FastAPI installed; FastAPI is imported only in
:mod:`culture_library.server.app` and the ``routes_*`` modules.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8780


@dataclass(frozen=True)
class ServerConfig:
    """Runtime configuration for the server (grows as route tasks land)."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "ServerConfig"]
