"""No response is cached after auth: data is no-store, the site revalidates every load."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from culture_library.server import ServerConfig  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CULTURE_LIBRARY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CULTURE_LIBRARY_PURGE_INTERVAL_S", "0")
    return TestClient(server_app.create_app(ServerConfig()))


@pytest.mark.parametrize("path", ["/api/health", "/api/library", "/opds", "/dav/"])
def test_data_paths_are_never_stored(client, path) -> None:
    r = client.get(path)
    assert r.headers["cache-control"] == "no-store, private"


@pytest.mark.parametrize("path", ["/", "/static/app.js", "/static/styles.css"])
def test_site_revalidates_on_every_load(client, path) -> None:
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "private, no-cache"
    assert "Authorization" in r.headers["vary"]


def test_errors_are_not_stored_either(client) -> None:
    assert client.get("/static/missing.js").headers["cache-control"] == "private, no-cache"
    assert client.get("/api/library").status_code == 401
