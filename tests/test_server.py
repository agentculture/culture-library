from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi import APIRouter  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.cli import main  # noqa: E402
from culture_library.server import ServerConfig  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402


def test_health() -> None:
    r = TestClient(server_app.create_app(ServerConfig())).get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_router_autodiscovery(monkeypatch: pytest.MonkeyPatch) -> None:
    router = APIRouter()

    @router.get("/api/ping")
    def ping() -> dict:
        return {"pong": True}

    monkeypatch.setattr(server_app, "_discover_routers", lambda: [router])
    assert TestClient(server_app.create_app()).get("/api/ping").json() == {"pong": True}


def test_discover_finds_routes_modules(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import culture_library.server as pkg

    (tmp_path / "routes_demo.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n"
        "@router.get('/api/demo')\ndef demo():\n    return {'ok': 1}\n"
    )
    monkeypatch.setattr(pkg, "__path__", [*pkg.__path__, str(tmp_path)])
    assert TestClient(server_app.create_app()).get("/api/demo").json() == {"ok": 1}


def test_serve_invokes_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {}
    monkeypatch.setattr(server_app, "serve", lambda cfg: calls.update(cfg=cfg))
    assert main(["server", "serve", "--host", "127.0.0.1", "--port", "9"]) == 0
    assert calls["cfg"] == ServerConfig(host="127.0.0.1", port=9)


def test_server_overview_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["server", "overview", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["subject"] == "culture-library server"


def test_explain_server(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["explain", "server"]) == 0
    assert "[server]" in capsys.readouterr().out
