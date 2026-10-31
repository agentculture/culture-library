"""Plan risk r6 at the routes: search, RAG retrieve, chunk listing and re-anchoring select
the (model id, strategy) the query embedder states, the pair the indexer writes.

The fake embedder states a 4-dimension space under another model id, so a route still
asking for the current pair (768 dimensions) finds nothing or fails. No gateway is contacted.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from culture_library.core import Store
from culture_library.core import indexer as ix
from culture_library.core import search as sr
from culture_library.server import app as server_app
from culture_library.server.auth import Principal, current_principal
from tests.core.search_helpers import drop_created_hnsw_indexes
from tests.core.test_query_space import OTHER, OTHER_MODEL

READ = frozenset({"read", "write"})


def _vec(text: str) -> list[float]:
    return [1.0, 0.0, 0.1, 0.0] if "whale" in text.lower() else [0.0, 1.0, 0.1, 0.0]


class SpacedEmbed:
    def __init__(self, fail: bool = False):
        self.fail = fail

    def vector_space(self):
        if self.fail:
            raise sr.EmbeddingError("capabilities unreachable")
        return OTHER_MODEL, OTHER

    def __call__(self, texts):
        return [_vec(t) for t in texts]


@pytest.fixture(autouse=True)
def _drop_created_hnsw_indexes(tmp_path):
    yield
    drop_created_hnsw_indexes(Store(tmp_path / "data"))


def _client(tmp_path, query_embedder):
    app = server_app.create_app()
    store = app.state.store = Store(tmp_path / "data")
    app.state.query_embedder = query_embedder
    app.state.reranker = None
    app.dependency_overrides[current_principal] = lambda: Principal("k", READ, private=True)
    return store, TestClient(app)


def _seed(store):
    whale = store.add_item(b"a whale tale", kind="book", title="Leviathan", format="txt").item
    store.add_item(b"flowers in spring", kind="book", title="Tulips", format="txt")
    ix.enqueue_missing(store)
    ix.Indexer(store, SpacedEmbed()).run_once()  # writes under the embedder's own pair
    with store.reader() as conn:
        pairs = conn.execute("SELECT DISTINCT model_id FROM embeddings").fetchall()
    assert [p[0] for p in pairs] == [OTHER_MODEL]
    return whale


def test_search_semantic_uses_the_query_embedders_space(tmp_path):
    store, c = _client(tmp_path, SpacedEmbed())
    whale = _seed(store)
    r = c.get("/api/search?q=whale&mode=semantic&limit=1")
    assert r.status_code == 200, r.text
    assert [h["item_id"] for h in r.json()["hits"]] == [whale.id]


def test_search_semantic_503_when_the_space_cannot_be_resolved(tmp_path):
    store, c = _client(tmp_path, SpacedEmbed(fail=True))
    _seed(store)
    r = c.get("/api/search?q=whale&mode=semantic")
    assert r.status_code == 503
    hybrid = c.get("/api/search?q=whale&mode=hybrid")
    assert hybrid.status_code == 200
    # degraded to full text, as with the gateway down (mode echoes the request)
    assert {h["source"] for h in hybrid.json()["hits"]} == {"fulltext"}


def test_rag_retrieve_uses_the_query_embedders_space(tmp_path):
    store, c = _client(tmp_path, SpacedEmbed())
    whale = _seed(store)
    for mode in ("semantic", "hybrid"):
        r = c.post("/api/rag/retrieve", json={"query": "whale", "k": 1, "mode": mode})
        assert r.status_code == 200, r.text
        assert [p["item_id"] for p in r.json()["passages"]] == [whale.id]


def test_rag_retrieve_503_when_the_space_cannot_be_resolved(tmp_path):
    store, c = _client(tmp_path, SpacedEmbed(fail=True))
    _seed(store)
    r = c.post("/api/rag/retrieve", json={"query": "whale"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "rag_unavailable"


def test_item_chunks_lists_the_query_embedders_space(tmp_path):
    store, c = _client(tmp_path, SpacedEmbed())
    whale = _seed(store)
    body = c.get(f"/api/rag/items/{whale.id}/chunks").json()
    assert body["embed_model"] == OTHER_MODEL
    assert any("whale" in ch["text"] for ch in body["chunks"])


def test_item_chunks_without_a_query_embedder_list_the_current_pair(tmp_path):
    store, c = _client(tmp_path, None)
    whale = _seed(store)  # vectors only under the other pair
    body = c.get(f"/api/rag/items/{whale.id}/chunks").json()
    assert body["chunks"] == []
    assert body["embed_model"] is None


def test_item_chunks_503_when_the_space_cannot_be_resolved(tmp_path):
    store, c = _client(tmp_path, SpacedEmbed(fail=True))
    whale = _seed(store)
    r = c.get(f"/api/rag/items/{whale.id}/chunks")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "rag_unavailable"


def _eval_set(c, item):
    chunks = c.get(f"/api/rag/items/{item.id}/chunks").json()["chunks"]
    body = {
        "item_id": item.id,
        "item_sha256": item.sha256,
        "adapter": "fake",
        "adapter_version": "0",
        "samples": [
            {
                "question": "what is the tale about?",
                "reference_answer": "a whale",
                "reference_contexts": [chunks[1]["text"]],
                "reference_chunk_shas": [chunks[1]["chunk_sha"]],
            }
        ],
    }
    r = c.post("/api/eval/sets", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_reanchor_reads_the_query_embedders_space(tmp_path):
    store, c = _client(tmp_path, SpacedEmbed())
    whale = _seed(store)
    set_id = _eval_set(c, whale)
    r = c.post(f"/api/eval/sets/{set_id}/reanchor")
    assert r.status_code == 200, r.text
    c.app.state.query_embedder = SpacedEmbed(fail=True)
    r = c.post(f"/api/eval/sets/{set_id}/reanchor")
    assert r.status_code == 503
    assert r.json()["code"] == "rag_unavailable"
