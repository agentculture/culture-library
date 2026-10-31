"""LibraryClient.get_chunks / retrieve against the real /api/rag routes."""

from __future__ import annotations

from culture_library.core import indexer as ix
from culture_library.core import search as sr


def _pad(v):  # vectors have the current strategy's 768 dimensions (decision c42)
    return [*v, *[0.0] * (768 - len(v))]


def _embed(texts):
    return [_pad([1.0, 0.0] if "whale" in t.lower() else [0.0, 1.0]) for t in texts]


def test_client_reads_rag_routes(library):
    library.app.state.query_embedder = _embed
    library.app.state.reranker = None
    item = library.add_item(b"a whale tale\n\nmore whale", title="Leviathan")
    with library.store.connect() as conn:
        sr.index_title(conn, item)
    ix.enqueue_missing(library.store)
    ix.Indexer(library.store, _embed).run_once()
    chunks = library.client.get_chunks(item.id)
    assert chunks
    assert chunks[0].is_header
    assert all(c.chunk_sha == sr.chunk_sha(c.text) for c in chunks)
    raw = library.client.request("GET", f"/api/rag/items/{item.id}/chunks")
    assert raw["item_sha256"] == item.sha256
    assert raw["index_state"] == "done"
    ps = library.client.retrieve("whale", k=2, mode="semantic")
    assert ps
    assert ps[0].item_id == item.id
    assert ps[0].rerank_score is None
