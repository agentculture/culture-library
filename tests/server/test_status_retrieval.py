"""GET /api/status ``retrieval`` block: reports the configured retrieval setup. No gateway."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store  # noqa: E402
from culture_library.core import search as sr  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402
from culture_library.server.auth import Principal, current_principal  # noqa: E402

READ = frozenset({"read"})


class _Configured:
    """Stands in for GatewayEmbedder / GatewayReranker: only the reported attributes matter."""

    def __init__(self, model, prompt_name=None):
        self.model = model
        self.prompt_name = prompt_name

    def __call__(self, *args, **kwargs):  # pragma: no cover - never called by status
        raise AssertionError("status must not call the gateway")


def _client(tmp_path, principal=None, **state):
    app = server_app.create_app()
    store = app.state.store = Store(tmp_path / "data")
    for name, value in state.items():
        setattr(app.state, name, value)
    if principal is not None:
        app.dependency_overrides[current_principal] = lambda: principal
    return store, TestClient(app)


def _index(store, title, visibility, model, chunks):
    res = store.add_item(
        title.encode(), kind="book", title=title, format="txt", visibility=visibility
    )
    with store.connect() as conn:
        sr.set_embeddings(conn, res.item.id, chunks, [[1.0, 0.0]] * len(chunks), model)


def _retrieval(store_client):
    return store_client.get("/api/status").json()["retrieval"]


def test_retrieval_all_null_without_embedders(tmp_path):
    store, c = _client(tmp_path, Principal(key_id="k", scopes=READ, private=True))
    r = _retrieval(c)
    assert r["embed_model"] is None
    assert r["embed_prompts"] is None
    assert r["reranker_model"] is None
    assert r["chunk_count"] == 0
    assert r["embed_models_stored"] == []
    assert r["chunker_max_chars"] == sr.CHUNK_MAX_CHARS == sr.chunk_text.__defaults__[0]


def test_retrieval_reports_configured_objects(tmp_path):
    doc = _Configured("gemma2-embed", "Retrieval-document")
    query = _Configured("gemma2-embed", "Retrieval-query")
    rr = _Configured("bge-reranker")
    store, c = _client(
        tmp_path,
        Principal(key_id="k", scopes=READ, private=True),
        doc_embedder=doc,
        query_embedder=query,
        reranker=rr,
    )
    _index(store, "Moby", "public", "gemma2-embed+Retrieval-document", ["a", "b"])
    _index(store, "Diary", "private", "gemma2-embed+Retrieval-document", ["c"])
    r = _retrieval(c)
    assert r["embed_model"] == doc.model
    assert r["embed_prompts"] == {"document": "Retrieval-document", "query": "Retrieval-query"}
    assert r["reranker_model"] == rr.model
    assert r["chunker_max_chars"] == sr.CHUNK_MAX_CHARS
    assert r["chunk_count"] == 3
    assert r["embed_models_stored"] == ["gemma2-embed+Retrieval-document"]


def test_retrieval_prompts_off_is_null(tmp_path):
    store, c = _client(
        tmp_path,
        Principal(key_id="k", scopes=READ, private=True),
        doc_embedder=_Configured("plain-embed", None),
        query_embedder=_Configured("plain-embed", None),
    )
    r = _retrieval(c)
    assert r["embed_model"] == "plain-embed"
    assert r["embed_prompts"] is None
    assert r["reranker_model"] is None


def test_retrieval_mixed_models_are_listed(tmp_path):
    store, c = _client(
        tmp_path,
        Principal(key_id="k", scopes=READ, private=True),
        doc_embedder=_Configured("new-embed", "Retrieval-document"),
        query_embedder=_Configured("new-embed", "Retrieval-query"),
    )
    _index(store, "Old", "public", "old-embed", ["a"])
    _index(store, "New", "public", "new-embed+Retrieval-document", ["b", "c"])
    r = _retrieval(c)
    assert r["chunk_count"] == 3
    assert r["embed_models_stored"] == ["new-embed+Retrieval-document", "old-embed"]


def test_retrieval_public_caller_sees_public_chunks_only(tmp_path):
    store, c = _client(
        tmp_path,
        Principal(key_id="k", scopes=READ, private=False),
        doc_embedder=_Configured("e", "Retrieval-document"),
    )
    _index(store, "Moby", "public", "e+Retrieval-document", ["a", "b"])
    _index(store, "Diary", "private", "secret-model", ["c"])
    r = _retrieval(c)
    assert r["chunk_count"] == 2
    assert r["embed_models_stored"] == ["e+Retrieval-document"]
    assert r["embed_model"] == "e"


def test_existing_status_fields_unchanged(tmp_path):
    store, c = _client(tmp_path, Principal(key_id="k", scopes=READ, private=True))
    body = c.get("/api/status").json()
    assert {"private_included", "counts", "index", "last_push", "feed"} <= set(body)
    assert "retrieval" in body
