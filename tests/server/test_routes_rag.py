"""Tests for POST /api/rag/retrieve and GET /api/rag/items/{id}/chunks. No gateway is contacted:
the query embedder and reranker are fakes injected on ``app.state``."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store  # noqa: E402
from culture_library.core import uploaders  # noqa: E402
from culture_library.core import indexer as ix  # noqa: E402
from culture_library.core import search as sr  # noqa: E402
from culture_library.gateway import RerankError  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402
from culture_library.server.auth import Principal, current_principal  # noqa: E402

READ = frozenset({"read"})
_NO_RERANKER = object()


def _vec(text):
    return [1.0, 0.0] if "whale" in text.lower() else [0.0, 1.0]


def _embed(texts):
    return [_vec(t) for t in texts]


def _down(texts):
    raise sr.EmbeddingError("gateway down")


class FakeReranker:
    """Scores documents by how many times 'harpoon' appears (best first, like vLLM)."""

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def __call__(self, query, documents):
        self.calls.append((query, list(documents)))
        if self.fail:
            raise RerankError("rerank gateway down")
        scored = [(i, float(d.count("harpoon"))) for i, d in enumerate(documents)]
        return sorted(scored, key=lambda s: -s[1])


def _client(tmp_path, principal, embedder=_embed, reranker=_NO_RERANKER):
    app = server_app.create_app()
    store = app.state.store = Store(tmp_path / "data")
    app.state.query_embedder = embedder
    app.state.reranker = None if reranker is _NO_RERANKER else reranker
    app.dependency_overrides[current_principal] = lambda: principal
    return store, TestClient(app)


def _add(store, title, body, kind="book", visibility="public", owner=None):
    res = store.add_item(body, kind=kind, title=title, format="txt", visibility=visibility)
    with store.connect() as conn:
        sr.index_title(conn, res.item)
        if owner is not None:
            uploaders.record(conn, res.item.id, owner)
    return res.item


def _index(store):
    ix.enqueue_missing(store)
    ix.Indexer(store, _embed).run_once()


def _seed(store):
    a = _add(store, "Leviathan", b"a whale tale\n\nwith a harpoon harpoon")
    b = _add(store, "Tulips", b"flowers in the spring")
    c = _add(store, "Whale news", b"whale sighted harpoon", kind="newspaper")
    _index(store)
    return a, b, c


# ------------------------------------------------------------------ retrieve


def test_retrieve_shape_and_order(tmp_path):
    store, c = _client(tmp_path, Principal("k", READ))
    a, b, n = _seed(store)
    r = c.post("/api/rag/retrieve", json={"query": "whale", "k": 3, "mode": "semantic"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["query"] == "whale"
    assert body["mode"] == "semantic"
    assert body["reranked"] is False
    ps = body["passages"]
    assert len(ps) == 3
    assert set(ps[0]) == {
        "item_id",
        "title",
        "kind",
        "chunk_no",
        "chunk_sha",
        "text",
        "score",
        "rerank_score",
        "source",
        "corpus",
    }
    assert ps[0]["item_id"] in (a.id, n.id)
    assert "whale" in ps[0]["text"].lower()
    assert ps[0]["chunk_sha"] == sr.chunk_sha(ps[0]["text"])
    assert ps[0]["rerank_score"] is None
    assert ps[0]["source"] == "semantic"
    scores = [p["score"] for p in ps]
    assert scores == sorted(scores, reverse=True)


def test_retrieve_hybrid_default_and_filters(tmp_path):
    store, c = _client(tmp_path, Principal("k", READ))
    a, b, n = _seed(store)
    body = c.post("/api/rag/retrieve", json={"query": "whale", "k": 10}).json()
    assert body["mode"] == "hybrid"
    assert {p["source"] for p in body["passages"]} == {"hybrid"}
    only = c.post("/api/rag/retrieve", json={"query": "whale", "k": 10, "kind": "newspaper"}).json()
    assert {p["item_id"] for p in only["passages"]} == {n.id}
    ids = c.post("/api/rag/retrieve", json={"query": "whale", "k": 10, "item_ids": [b.id]}).json()
    assert {p["item_id"] for p in ids["passages"]} == {b.id}


def test_retrieve_validation_and_scope(tmp_path):
    _, c = _client(tmp_path, Principal("k", READ))
    post = c.post
    assert post("/api/rag/retrieve", json={"k": 3}).status_code == 422
    assert post("/api/rag/retrieve", json={"query": "x", "k": 0}).status_code == 422
    assert post("/api/rag/retrieve", json={"query": "x", "k": 51}).status_code == 422
    assert post("/api/rag/retrieve", json={"query": "x", "mode": "fulltext"}).status_code == 422
    assert post("/api/rag/retrieve", json={"query": "x", "kind": "bogus"}).status_code == 422
    bad = {"query": "x", "k": 10, "candidates": 5}  # candidates below k
    assert post("/api/rag/retrieve", json=bad).status_code == 422
    assert post("/api/rag/retrieve", json={"query": "x", "candidates": 201}).status_code == 422
    _, c = _client(tmp_path / "w", Principal("k", frozenset({"write"})))
    assert c.post("/api/rag/retrieve", json={"query": "x"}).status_code == 403


def test_retrieve_unavailable_without_query_embedder(tmp_path):
    _, c = _client(tmp_path, Principal("k", READ), embedder=None)
    r = c.post("/api/rag/retrieve", json={"query": "whale"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "rag_unavailable"


def test_retrieve_unavailable_when_embedder_down(tmp_path):
    _, c = _client(tmp_path, Principal("k", READ), embedder=_down)
    r = c.post("/api/rag/retrieve", json={"query": "whale"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "rag_unavailable"


def test_retrieve_ignores_search_embedder_override(tmp_path):
    """``app.state.embedder`` is /api/search's override; RAG uses the query embedder only."""
    _, c = _client(tmp_path, Principal("k", READ), embedder=None)
    c.app.state.embedder = _embed
    assert c.post("/api/rag/retrieve", json={"query": "whale"}).status_code == 503


# ------------------------------------------------------------------ rerank


def test_rerank_reorders_and_truncates(tmp_path):
    rr = FakeReranker()
    store, c = _client(tmp_path, Principal("k", READ), reranker=rr)
    _seed(store)
    r = c.post("/api/rag/retrieve", json={"query": "whale", "k": 2, "mode": "semantic"})
    body = r.json()
    assert body["reranked"] is True  # defaults on when a reranker is configured
    ps = body["passages"]
    assert len(ps) == 2
    assert ps[0]["text"].count("harpoon") == 2
    assert [p["rerank_score"] for p in ps] == [2.0, 1.0]
    q, docs = rr.calls[0]
    assert q == "whale"
    assert len(docs) > 2  # a wider candidate window was reranked


def test_rerank_candidates_window(tmp_path):
    rr = FakeReranker()
    store, c = _client(tmp_path, Principal("k", READ), reranker=rr)
    _seed(store)
    c.post("/api/rag/retrieve", json={"query": "whale", "k": 1, "candidates": 2})
    assert len(rr.calls[0][1]) == 2


def test_rerank_opt_out(tmp_path):
    rr = FakeReranker()
    store, c = _client(tmp_path, Principal("k", READ), reranker=rr)
    _seed(store)
    body = c.post("/api/rag/retrieve", json={"query": "whale", "k": 2, "rerank": False}).json()
    assert body["reranked"] is False
    assert rr.calls == []
    assert all(p["rerank_score"] is None for p in body["passages"])


def test_rerank_requested_without_reranker(tmp_path):
    store, c = _client(tmp_path, Principal("k", READ))
    _seed(store)
    body = c.post("/api/rag/retrieve", json={"query": "whale", "k": 2, "rerank": True}).json()
    assert body["reranked"] is False
    assert len(body["passages"]) == 2


@pytest.mark.parametrize("exc", [RerankError("down"), RuntimeError("boom")])
def test_rerank_failure_keeps_original_order(tmp_path, exc):
    def broken(query, documents):
        raise exc

    store, c = _client(tmp_path, Principal("k", READ), reranker=broken)
    _seed(store)
    plain = c.post(
        "/api/rag/retrieve", json={"query": "whale", "k": 2, "mode": "semantic", "rerank": False}
    ).json()
    r = c.post("/api/rag/retrieve", json={"query": "whale", "k": 2, "mode": "semantic"})
    assert r.status_code == 200
    body = r.json()
    assert body["reranked"] is False
    assert body["passages"] == plain["passages"]


def test_rerank_partial_results_keep_unscored_after(tmp_path):
    def top_one(query, documents):
        return [(len(documents) - 1, 9.0)]

    store, c = _client(tmp_path, Principal("k", READ), reranker=top_one)
    _seed(store)
    plain = c.post(
        "/api/rag/retrieve",
        json={"query": "whale", "k": 3, "mode": "semantic", "rerank": False, "candidates": 3},
    ).json()["passages"]
    body = c.post(
        "/api/rag/retrieve", json={"query": "whale", "k": 3, "mode": "semantic", "candidates": 3}
    ).json()
    ps = body["passages"]
    assert body["reranked"] is True
    assert ps[0]["chunk_sha"] == plain[2]["chunk_sha"]
    assert ps[0]["rerank_score"] == 9.0
    assert [p["chunk_sha"] for p in ps[1:]] == [p["chunk_sha"] for p in plain[:2]]
    assert all(p["rerank_score"] is None for p in ps[1:])


# ------------------------------------------------------------------ item chunks


def test_item_chunks(tmp_path):
    store, c = _client(tmp_path, Principal("k", READ))
    a, _, _ = _seed(store)
    r = c.get(f"/api/rag/items/{a.id}/chunks")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["item_id"] == a.id
    assert body["item_sha256"] == a.sha256
    assert body["index_state"] == "done"
    assert body["embed_model"] is not None
    chunks = body["chunks"]
    assert [ch["chunk_no"] for ch in chunks] == list(range(len(chunks)))
    assert set(chunks[0]) == {"chunk_no", "chunk_sha", "text", "is_header"}
    assert chunks[0]["is_header"] is True
    assert all(not ch["is_header"] for ch in chunks[1:])
    assert all(ch["chunk_sha"] == sr.chunk_sha(ch["text"]) for ch in chunks)
    assert any("whale" in ch["text"] for ch in chunks[1:])


def test_item_chunks_unindexed(tmp_path):
    store, c = _client(tmp_path, Principal("k", READ))
    a = _add(store, "Fresh", b"not yet")
    body = c.get(f"/api/rag/items/{a.id}/chunks").json()
    assert body["index_state"] == "none"
    assert body["embed_model"] is None
    assert body["chunks"] == []
    ix.enqueue_missing(store)
    assert c.get(f"/api/rag/items/{a.id}/chunks").json()["index_state"] == "pending"


def test_item_chunks_failed_state(tmp_path):
    store, c = _client(tmp_path, Principal("k", READ))
    a = _add(store, "Broken", b"x")
    ix.enqueue_missing(store)
    ix.Indexer(store, _down, max_attempts=1).run_once()
    assert c.get(f"/api/rag/items/{a.id}/chunks").json()["index_state"] == "failed"


def test_item_chunks_404s_and_scope(tmp_path):
    store, c = _client(tmp_path, Principal("k", READ))
    hidden = _add(store, "Whale hidden", b"whale secret", visibility="private")
    gone = _add(store, "Whale archived", b"whale gone")
    _index(store)
    with store.connect() as conn:
        conn.execute("UPDATE items SET archived_at='2026-01-01T00:00:00Z' WHERE id=?", (gone.id,))
    for iid in (hidden.id, gone.id, 9999):
        r = c.get(f"/api/rag/items/{iid}/chunks")
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "not_found"
    _, w = _client(tmp_path / "w", Principal("k", frozenset({"write"})))
    assert w.get(f"/api/rag/items/{hidden.id}/chunks").status_code == 403


# ------------------------------------------------------------------ visibility parity


@pytest.mark.parametrize(
    "principal",
    [
        Principal("pub", READ),
        Principal("priv", READ, private=True),
        Principal("owner", READ),
    ],
    ids=["public", "private", "owner"],
)
def test_visibility_parity_with_search(tmp_path, principal):
    store, c = _client(tmp_path, principal)
    c.app.state.embedder = _embed  # /api/search's override, same fake vectors
    items = {
        "open": _add(store, "Whale open", b"whale open"),
        "hidden": _add(store, "Whale hidden", b"whale hidden", visibility="private"),
        "mine": _add(store, "Whale mine", b"whale mine", visibility="private", owner="owner"),
        "gone": _add(store, "Whale gone", b"whale gone"),
    }
    _index(store)
    with store.connect() as conn:
        conn.execute(
            "UPDATE items SET archived_at='2026-01-01T00:00:00Z' WHERE id=?",
            (items["gone"].id,),
        )
    for mode in ("semantic", "hybrid"):
        hits = c.get(f"/api/search?q=whale&mode={mode}&limit=100").json()["hits"]
        search_ids = {h["item_id"] for h in hits}
        rag = c.post("/api/rag/retrieve", json={"query": "whale", "k": 50, "mode": mode}).json()
        rag_ids = {p["item_id"] for p in rag["passages"]}
        assert rag_ids == search_ids, mode
    expected = {"open"} | {"pub": set(), "priv": {"hidden", "mine"}, "owner": {"mine"}}[
        principal.key_id
    ]
    assert rag_ids == {items[n].id for n in expected}
    for name, item in items.items():
        status = c.get(f"/api/rag/items/{item.id}/chunks").status_code
        assert status == (200 if name in expected else 404), name
