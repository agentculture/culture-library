"""GET /api/status ``database`` block and its behaviour with Postgres stopped.

Uses a router-only app so it does not depend on every route module being importable.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from culture_library.core import Store, migrations
from culture_library.core.embedding_strategy import (
    CURRENT_MODEL_ID,
    CURRENT_STRATEGY,
    hnsw_index_name,
)
from culture_library.core.store import DatabaseError
from culture_library.server import routes_status
from culture_library.server.auth import Principal, current_principal

READ = frozenset({"read"})


def _principal():
    return Principal(key_id="k", scopes=READ, private=True)


def _app(store):
    app = FastAPI()
    app.include_router(routes_status.router)
    app.state.store = store
    app.dependency_overrides[current_principal] = _principal
    return app


def test_database_block_reports_version_and_default_index(tmp_path):
    store = Store(tmp_path / "data")
    block = routes_status.database_block(store)
    assert block["reachable"] is True
    assert block["schema_version"] == migrations.CURRENT_VERSION
    assert block["expected_schema_version"] == migrations.CURRENT_VERSION
    assert block["schema_current"] is True
    emb = block["vector_indexes"]["embeddings"]
    [index] = emb["indexes"]
    assert index["name"] == hnsw_index_name("embeddings", CURRENT_MODEL_ID, CURRENT_STRATEGY)
    assert index["strategy"] == CURRENT_STRATEGY.key()
    assert index["valid"] is True
    assert emb["unindexed"] == []
    assert set(block["vector_indexes"]) == {"embeddings", "repodoc_embeddings"}


def test_unindexed_pair_is_listed(tmp_path):
    store = Store(tmp_path / "data")
    item = store.add_item(b"x", kind="book", title="t", format="txt").item
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO embeddings (item_id, model_id, strategy, chunk_no, text, vec) "
            "VALUES (%s, 'other-model', %s, 0, 'x', %s::vector)",
            (item.id, CURRENT_STRATEGY.key(), "[" + ",".join(["0.1"] * 768) + "]"),
        )
    emb = routes_status.database_block(store)["vector_indexes"]["embeddings"]
    assert emb["unindexed"] == [{"model_id": "other-model", "strategy": CURRENT_STRATEGY.key()}]


def test_status_has_database_block_with_database_up(tmp_path, monkeypatch):
    # indexer, search and the mirror route are ported by other tasks; status only calls them
    # for their own blocks, so stand-ins keep this test about the status SQL and the new block.
    stubs = {
        "culture_library.core.indexer": SimpleNamespace(status=lambda store: {}),
        "culture_library.core.search": SimpleNamespace(CHUNK_MAX_CHARS=1000),
        "culture_library.server.routes_mirror": SimpleNamespace(status_block=lambda *a: {}),
    }
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)
        monkeypatch.setattr(
            sys.modules[name.rsplit(".", 1)[0]], name.rsplit(".", 1)[1], module, raising=False
        )
    http = TestClient(_app(Store(tmp_path / "data")))
    body = http.get("/api/status").json()
    assert body["database"]["reachable"] is True
    assert body["feed"] == {"cursor": 0}
    assert body["counts"]["items"]["total"] == 0


def test_status_answers_with_database_stopped(tmp_path):
    store = Store(tmp_path / "data")
    # Same store, DSN now pointing at a closed port: the database is "stopped".
    store._conninfo = "postgresql://postgres@127.0.0.1:1/postgres?connect_timeout=2"
    http = TestClient(_app(store))
    resp = http.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["database"]["reachable"] is False
    assert body["database"]["schema_version"] is None
    assert body["counts"] is None
    assert body["feed"] is None
    assert "retrieval" in body
    assert body["retrieval"]["chunk_count"] is None


def test_status_with_unverifiable_key_and_database_stopped(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    store._conninfo = "postgresql://postgres@127.0.0.1:1/postgres?connect_timeout=2"
    app = FastAPI()
    app.include_router(routes_status.router)
    app.state.store = store

    def _down(_store, _raw):  # what keys.authenticate does when it cannot reach Postgres
        raise DatabaseError("cannot connect")

    monkeypatch.setattr("culture_library.core.keys.authenticate", _down)
    resp = TestClient(app).get("/api/status", headers={"Authorization": "Bearer lib_x"})
    assert resp.status_code == 200
    assert resp.json()["database"]["reachable"] is False


def test_missing_credentials_still_401_with_database_stopped(tmp_path):
    store = Store(tmp_path / "data")
    store._conninfo = "postgresql://postgres@127.0.0.1:1/postgres?connect_timeout=2"
    app = FastAPI()
    app.include_router(routes_status.router)
    app.state.store = store
    assert TestClient(app).get("/api/status").status_code == 401
