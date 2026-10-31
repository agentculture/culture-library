"""/api/health fails when the database is unreachable (plan task t10, claim c40)."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store  # noqa: E402
from culture_library.core.store import DatabaseError  # noqa: E402
from culture_library.server.app import health_router  # noqa: E402


def _client(store) -> TestClient:
    app = FastAPI()
    app.include_router(health_router)
    app.state.store = store
    return TestClient(app)


class _DownStore:
    @contextmanager
    def reader(self):
        raise DatabaseError("database unreachable: connection refused")
        yield  # pragma: no cover


def test_health_is_ok_with_the_same_body_when_the_database_answers(tmp_path):
    response = _client(Store(tmp_path / "data")).get("/api/health")
    assert response.status_code == 200
    assert set(response.json()) == {"status", "version", "api_version"}
    assert response.json()["status"] == "ok"


def test_health_is_503_when_the_database_is_unreachable():
    response = _client(_DownStore()).get("/api/health")
    assert response.status_code == 503
    assert response.json()["status"] == "error"
    assert "database" in response.json()["detail"]
