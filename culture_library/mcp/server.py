"""The MCP server (official ``mcp`` SDK, low-level ``Server`` + ``mcp.types``).

Provenance: cited (cite-don't-import) from agentculture/xteink @ ea2f643,
``xteink/mcp/server.py``; this copy is owned here. Modified: ``clk_`` keys, env names, a
bind guard (no wildcard interface unless asked).

Transports:

* stdio: talks to the HTTP API with the key from ``CULTURE_LIBRARY_API_KEY`` (a local,
  single-user process).
* streamable-http: every request must carry ``Authorization: Bearer clk_...``; requests
  without one get 401 before reaching the MCP session, and each tool call uses the caller's
  own key against the API, which decides scopes. This server holds no key of its own, and
  ``push_file``'s ``path`` (a file on the MCP host) is refused over HTTP.

LAN/loopback only: this is a separate process (``python -m culture_library.mcp``), never mounted
into the public site app and never routed through the public hostname.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from culture_library.mcp import client as api_client
from culture_library.mcp import tools

__all__ = ["ServerExtraMissing", "build_server", "run_http", "run_stdio"]

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8797
_WILDCARDS = {"0.0.0.0", "::", ""}  # nosec B104
_EXTRA_MESSAGE = "the MCP server needs the 'server' extra: pip install 'culture-library[server]'"


class ServerExtraMissing(ImportError):
    """The ``server`` extra (mcp SDK) is not installed."""


def _env() -> dict[str, str]:
    return dict(os.environ)


def _sdk():
    try:
        import mcp.types as types  # noqa: PLC0415 - optional extra
        from mcp.server.lowlevel import Server  # noqa: PLC0415
    except ImportError as exc:
        raise ServerExtraMissing(_EXTRA_MESSAGE) from exc
    return Server, types


def _bearer(headers: Any) -> str:
    auth = headers.get("authorization") or ""
    scheme, _, key = auth.partition(" ")
    return key.strip() if scheme.lower() == "bearer" else ""


def build_server(client_factory: Callable[[], Any] | None = None, *, http_auth: bool = False):
    """The MCP server. With ``http_auth`` every tool call uses the HTTP caller's bearer key."""
    server_cls, types = _sdk()
    default_factory = client_factory or api_client.from_env
    server = server_cls("culture-library")

    def factory_for_call(arguments: dict[str, Any] | None) -> Callable[[], Any]:
        if not http_auth:
            return default_factory
        if arguments and arguments.get("path") is not None:
            raise tools.ToolError(
                "invalid_arguments: path (a file on the MCP host) is not allowed over HTTP; "
                "send content or content_base64"
            )
        key = _bearer(server.request_context.request.headers)
        if not key:
            raise tools.ToolError("missing_key: send Authorization: Bearer clk_...")
        base_url = api_client.from_env({**_env(), api_client.KEY_ENV: key}).base_url
        return lambda: api_client.Client(base_url, key)

    @server.list_tools()
    async def _list() -> list[Any]:
        return [types.Tool(**spec) for spec in tools.tool_specs()]

    @server.call_tool()
    async def _call(name: str, arguments: dict[str, Any] | None) -> list[Any]:
        import anyio  # noqa: PLC0415

        try:
            factory = factory_for_call(arguments)
            # urllib is blocking; keep it off the event loop.
            result = await anyio.to_thread.run_sync(tools.call_tool, name, arguments, factory)
        except tools.ToolError as exc:
            raise ValueError(str(exc)) from exc  # the SDK turns this into isError
        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    return server


def run_stdio() -> None:
    try:
        import anyio  # noqa: PLC0415
        from mcp.server.stdio import stdio_server  # noqa: PLC0415
    except ImportError as exc:
        raise ServerExtraMissing(_EXTRA_MESSAGE) from exc
    server = build_server()

    async def main() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(main)


def build_http_app():
    """The Starlette app serving ``/mcp`` (streamable-http) with the bearer guard."""
    try:
        import contextlib  # noqa: PLC0415

        from mcp.server.streamable_http_manager import (  # noqa: PLC0415
            StreamableHTTPSessionManager,
        )
        from starlette.applications import Starlette  # noqa: PLC0415
        from starlette.responses import JSONResponse  # noqa: PLC0415
        from starlette.routing import Mount  # noqa: PLC0415
    except ImportError as exc:
        raise ServerExtraMissing(_EXTRA_MESSAGE) from exc

    manager = StreamableHTTPSessionManager(app=build_server(http_auth=True))

    async def guarded(scope, receive, send) -> None:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if not _bearer(headers).startswith("clk_"):
            response = JSONResponse(
                {"detail": "send Authorization: Bearer clk_... (a culture-library API key)"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    return Starlette(routes=[Mount("/mcp", app=guarded)], lifespan=lifespan)


def check_bind(bind: str, *, allow_any_interface: bool = False) -> None:
    """Refuse a wildcard bind (it would listen on every interface, public ones included)."""
    if bind in _WILDCARDS and not allow_any_interface:
        raise ValueError(
            f"refusing to bind {bind or '(empty)'!r}: the MCP server is LAN/loopback only; "
            "give a specific LAN address, or --allow-any-interface behind a firewall"
        )


def run_http(bind: str = DEFAULT_BIND, port: int = DEFAULT_PORT, *, allow_any_interface=False):
    check_bind(bind, allow_any_interface=allow_any_interface)
    try:
        import uvicorn  # noqa: PLC0415
    except ImportError as exc:
        raise ServerExtraMissing(_EXTRA_MESSAGE) from exc
    uvicorn.run(build_http_app(), host=bind, port=port, log_level="info")
