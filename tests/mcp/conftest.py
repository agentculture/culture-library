"""Fixtures: the real app in-process (TestClient), keys minted into its store, no network."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store  # noqa: E402
from culture_library.core import keys as core_keys  # noqa: E402
from culture_library.mcp import client as api_client  # noqa: E402
from culture_library.server import ServerConfig  # noqa: E402
from culture_library.server import routes_send  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402

TARGETS = '[{"label":"x3-ori","enabled":true}]'


class Env:
    def __init__(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("CULTURE_LIBRARY_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("CULTURE_LIBRARY_PURGE_INTERVAL_S", "0")
        self.app = server_app.create_app(ServerConfig())
        self.app.state.store = self.store = Store(tmp_path / "data")
        self.http = TestClient(self.app)
        self.published: list[dict] = []
        monkeypatch.setattr(routes_send, "publish", lambda data: self.published.append(data))

    def key(self, name, scopes, *, kinds=None, private=False) -> str:
        return core_keys.mint(self.store, name, scopes, kinds=kinds, private=private)[1]

    def client(self, raw: str) -> api_client.Client:
        return api_client.Client("http://testserver", raw, transport=self._send)

    def factory(self, raw: str):
        return lambda: self.client(raw)

    def _send(self, method, url, headers, body):
        r = self.http.request(method, url, headers=headers, content=body)
        return r.status_code, r.content


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CULTURE_LIBRARY_SEND_TARGETS", TARGETS)
    return Env(tmp_path, monkeypatch)
