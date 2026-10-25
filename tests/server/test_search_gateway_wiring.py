"""routes_search wiring: the indexer gets the document embedder; app.state carries the query
embedder and the reranker. The gateway is never contacted."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store  # noqa: E402
from culture_library.core import search as sr  # noqa: E402
from culture_library.gateway import GatewayEmbedder, GatewayReranker  # noqa: E402
from culture_library.server import ServerConfig  # noqa: E402
from culture_library.server import routes_search  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402
from culture_library.server.auth import Principal, current_principal  # noqa: E402


@pytest.fixture
def gateway_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CULTURE_LIBRARY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CULTURE_LIBRARY_PURGE_INTERVAL_S", "0")
    # nothing listens on port 9, so the background indexer's calls just fail and retry
    monkeypatch.setenv("LOBES_GATEWAY_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("GATEWAY_API_KEY", "sekret")
    for var in ("LOBES_EMBED_PROMPTS", "LOBES_EMBED_MODEL", "LOBES_RERANK_MODEL"):
        monkeypatch.delenv(var, raising=False)
    yield monkeypatch
    routes_search.stop_indexer()


def test_start_indexer_default_uses_document_embedder(gateway_env, tmp_path):
    idx = routes_search.start_indexer(Store(tmp_path / "d"))
    assert isinstance(idx.embed, GatewayEmbedder)
    assert idx.embed.prompt_name == "Retrieval-document"
    assert idx.model == "gemma2-embed+Retrieval-document"  # recorded in embeddings.model


def test_start_indexer_records_plain_model_when_prompts_off(gateway_env, tmp_path):
    gateway_env.setenv("LOBES_EMBED_PROMPTS", "off")
    idx = routes_search.start_indexer(Store(tmp_path / "d"))
    assert idx.embed.prompt_name is None
    assert idx.model == "gemma2-embed"


def test_start_indexer_injected_plain_callable_keeps_model(tmp_path):
    def embed(texts):
        return [[1.0] for _ in texts]

    try:
        idx = routes_search.start_indexer(Store(tmp_path / "d"), embedder=embed)
        assert idx.model == ""
    finally:
        routes_search.stop_indexer()


def test_lifespan_sets_query_embedder_and_reranker(gateway_env):
    app = server_app.create_app(ServerConfig())
    with TestClient(app):
        doc, query, rr = app.state.doc_embedder, app.state.query_embedder, app.state.reranker
        assert isinstance(doc, GatewayEmbedder)
        assert doc.prompt_name == "Retrieval-document"
        assert isinstance(query, GatewayEmbedder)
        assert query.prompt_name == "Retrieval-query"
        assert isinstance(rr, GatewayReranker)
        assert rr.model == "reranker"
        assert doc.model == query.model == "gemma2-embed"


def test_lifespan_without_key_leaves_none(gateway_env):
    gateway_env.delenv("GATEWAY_API_KEY")
    app = server_app.create_app(ServerConfig())
    with TestClient(app):
        assert app.state.doc_embedder is None
        assert app.state.query_embedder is None
        assert app.state.reranker is None


def test_lifespan_keeps_injected_query_embedder_and_reranker(gateway_env):
    app = server_app.create_app(ServerConfig())
    app.state.query_embedder = q = object()
    app.state.reranker = r = object()
    with TestClient(app):
        assert app.state.query_embedder is q
        assert app.state.reranker is r


def _vec(text):
    return [1.0, 0.0] if "whale" in text else [0.0, 1.0]


def test_search_semantic_uses_query_embedder(tmp_path):
    seen = []

    def query_embed(texts):
        seen.extend(texts)
        return [_vec(t) for t in texts]

    app = server_app.create_app()
    store = app.state.store = Store(tmp_path / "data")
    app.state.query_embedder = query_embed
    app.dependency_overrides[current_principal] = lambda: Principal("k", frozenset({"read"}))
    res = store.add_item(b"whale", kind="book", title="Moby", format="txt", visibility="public")
    with store.connect() as conn:
        sr.set_embeddings(conn, res.item.id, ["a whale"], [[1.0, 0.0]], "m")
    r = TestClient(app).get("/api/search?q=whale&mode=semantic")
    assert r.status_code == 200
    assert [h["title"] for h in r.json()["hits"]] == ["Moby"]
    assert seen == ["whale"]


def test_search_explicit_embedder_override_wins(tmp_path):
    def never(texts):
        raise AssertionError("query embedder must not be used when embedder is injected")

    app = server_app.create_app()
    app.state.store = Store(tmp_path / "data")
    app.state.query_embedder = never
    app.state.embedder = None
    app.dependency_overrides[current_principal] = lambda: Principal("k", frozenset({"read"}))
    r = TestClient(app).get("/api/search?q=whale&mode=semantic")
    assert r.status_code == 503
