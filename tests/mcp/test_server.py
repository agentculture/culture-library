"""server.py without the SDK (bind guard, import hygiene) and with it when installed."""

from __future__ import annotations

import importlib.util
import json
import sys

import pytest

from culture_library.mcp import __main__ as entry
from culture_library.mcp import server

HAS_SDK = importlib.util.find_spec("mcp") is not None


def test_wildcard_bind_refused():
    for bad in ("0.0.0.0", "::", ""):
        with pytest.raises(ValueError, match="LAN/loopback"):
            server.check_bind(bad)
    server.check_bind("127.0.0.1")
    server.check_bind("192.168.1.5")
    server.check_bind("0.0.0.0", allow_any_interface=True)


def test_defaults_are_loopback():
    assert (server.DEFAULT_BIND, server.DEFAULT_PORT) == ("127.0.0.1", 8797)


def test_not_mounted_into_public_app():
    from culture_library.server import app as server_app

    paths = {getattr(r, "path", "") for r in server_app.create_app().routes}
    assert not any(p.startswith("/mcp") for p in paths)


def test_main_reports_missing_extra_or_bad_bind(capsys):
    assert entry.main(["--transport", "streamable-http", "--bind", "0.0.0.0"]) == 2
    assert "LAN/loopback" in capsys.readouterr().err


def test_server_needs_sdk_only_when_used(monkeypatch):
    # Simulate an install without the ``server`` extra even when the SDK is present.
    for name in ("mcp", "mcp.types", "mcp.server", "mcp.server.lowlevel"):
        monkeypatch.setitem(sys.modules, name, None)
    with pytest.raises(server.ServerExtraMissing):
        server.build_server()


@pytest.mark.skipif(not HAS_SDK, reason="needs the mcp SDK")
def test_http_app_requires_bearer():
    from starlette.testclient import TestClient

    app = server.build_http_app()
    with TestClient(app) as c:
        r = c.post("/mcp/", json={})
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        r = c.post("/mcp/", json={}, headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401


@pytest.mark.skipif(not HAS_SDK, reason="needs the mcp SDK")
def test_stdio_style_server_lists_and_calls_tools(env):
    import anyio
    from mcp.shared.memory import create_connected_server_and_client_session

    raw = env.key("agent", ["read", "write"], private=True)
    srv = server.build_server(env.factory(raw))

    async def run():
        async with create_connected_server_and_client_session(srv) as session:
            listed = await session.list_tools()
            assert {t.name for t in listed.tools} >= {"push_file", "diary_write"}
            res = await session.call_tool("diary_write", {"text": "hello"})
            assert not res.isError and json.loads(res.content[0].text)["item"]["kind"] == "diary"
            bad = await session.call_tool("read_item", {})
            assert bad.isError

    anyio.run(run)
