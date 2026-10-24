"""Search + indexer tests. The gateway is never contacted: embedders are stubs, and the
autouse no-network guard in conftest.py enforces it."""

import pytest

from culture_library.core import indexer as ix
from culture_library.core import search as sr


def _add(store, title, body=b"", *, vis="public", fmt="md", **kw):
    meta = dict(kind="document", title=title, format=fmt, visibility=vis)
    meta.update(kw)
    with_data = body or title.encode()
    res = store.add_item(with_data, **meta)
    with store.connect() as conn:
        ix.index_on_add(conn, res.item)
    return res.item


def fake_embed(texts):
    """Deterministic 3-d vectors: cats / dogs / other."""
    out = []
    for t in texts:
        low = t.lower()
        out.append([float("cat" in low or "feline" in low), float("dog" in low), 0.1])
    return out


class DownEmbed:
    def __init__(self):
        self.up = False
        self.calls = 0

    def __call__(self, texts):
        self.calls += 1
        if not self.up:
            raise ConnectionRefusedError("gateway down")
        return fake_embed(texts)


def test_title_searchable_immediately_without_indexer(store):
    _add(store, "Dune Messiah")
    hits = sr.fulltext(store, "dune")
    assert [h.title for h in hits] == ["Dune Messiah"]


def test_body_fulltext_with_snippet_after_indexing(store):
    item = _add(store, "Notes", b"the quick brown fox jumps over the lazy dog")
    assert sr.fulltext(store, "fox") == []
    ix.Indexer(store, embed=fake_embed).run_once()
    hits = sr.fulltext(store, "fox")
    assert hits[0].item_id == item.id
    assert "[fox]" in hits[0].snippet


def test_query_syntax_is_neutralised(store):
    _add(store, "Alpha beta")
    assert sr.fulltext(store, 'alpha" (') != []  # punctuation stripped, no FTS5 syntax error
    assert sr.fulltext(store, "") == []
    assert sr.fulltext(store, '"*^ -') == []


def test_prefix_match_on_last_term(store):
    _add(store, "Constitution of India")
    assert len(sr.fulltext(store, "constit")) == 1


def test_visibility_respected(store):
    _add(store, "Secret plans", vis="private")
    _add(store, "Public plans", vis="public")
    assert [h.title for h in sr.fulltext(store, "plans")] == ["Public plans"]
    both = sr.fulltext(store, "plans", include_private=True)
    assert {h.title for h in both} == {"Secret plans", "Public plans"}


def test_archived_hidden(store):
    item = _add(store, "Old thing")
    with store.connect() as conn:
        conn.execute("UPDATE items SET archived_at='2026-01-01' WHERE id=?", (item.id,))
    assert sr.fulltext(store, "old") == []


def test_semantic_ranking_and_visibility(store):
    cat = _add(store, "Felines", b"all about the cat family")
    dog = _add(store, "Canines", b"all about the dog family")
    priv = _add(store, "Private cats", b"cat cat cat", vis="private")
    ix.Indexer(store, embed=fake_embed).run_once()
    hits = sr.semantic(store, "cat", fake_embed)
    assert hits[0].item_id == cat.id
    assert priv.id not in [h.item_id for h in hits]
    assert dog.id in [h.item_id for h in hits]
    allhits = sr.semantic(store, "cat", fake_embed, include_private=True)
    assert priv.id in [h.item_id for h in allhits]


def test_hybrid_falls_back_to_fulltext_when_embedder_down(store):
    _add(store, "Felines", b"all about the cat family")
    ix.Indexer(store, embed=fake_embed).run_once()
    hits = sr.hybrid(store, "cat", DownEmbed())
    assert [h.title for h in hits] == ["Felines"]


def test_semantic_raises_when_embedder_down(store):
    embed = DownEmbed()
    with pytest.raises(sr.EmbeddingError):
        sr.semantic(store, "cat", embed)


def test_gateway_down_add_ok_title_searchable_then_catches_up(store):
    emb = DownEmbed()
    clock = [1000.0]
    idx = ix.Indexer(store, embed=emb, now=lambda: clock[0], backoff_base=10, backoff_cap=100)
    item = _add(store, "Felines", b"all about the cat family")
    # Upload succeeded; title searchable before any indexing.
    assert sr.fulltext(store, "felines")[0].item_id == item.id
    idx.run_once()
    # body indexed (no gateway needed) but semantic still pending
    assert sr.fulltext(store, "family")[0].item_id == item.id
    st = ix.status(store, now=clock[0])
    assert st["pending"] == 1
    assert st["done"] == 0
    assert st["last_error"]
    assert st["last_failure_at"] is not None
    assert st["last_success_at"] is None
    with pytest.raises(sr.EmbeddingError):
        sr.semantic(store, "cat", emb)
    # backoff: immediate re-run does nothing
    calls = emb.calls
    assert idx.run_once() == 0
    assert emb.calls == calls
    clock[0] += 11
    idx.run_once()
    assert emb.calls == calls + 1
    # exponential growth, capped
    clock[0] += 15  # < 20 second backoff
    assert idx.run_once() == 0
    # gateway recovers
    emb.up = True
    clock[0] += 200
    assert idx.run_once() == 1
    st = ix.status(store, now=clock[0])
    assert st["pending"] == 0
    assert st["done"] == 1
    assert st["failed"] == 0
    assert st["last_success_at"] is not None
    assert st["lag_seconds"] == 0
    assert sr.semantic(store, "cat", emb)[0].item_id == item.id


def test_gives_up_after_max_attempts_and_requeue(store):
    emb = DownEmbed()
    clock = [0.0]
    idx = ix.Indexer(store, embed=emb, now=lambda: clock[0], max_attempts=3, backoff_base=1)
    _add(store, "Felines", b"cat")
    for _ in range(5):
        clock[0] += 1000
        idx.run_once()
    st = ix.status(store, now=clock[0])
    assert st["failed"] == 1
    assert st["pending"] == 0
    emb.up = True
    assert ix.requeue_failed(store) == 1
    clock[0] += 1
    idx.run_once()
    assert ix.status(store, now=clock[0])["done"] == 1


def test_lag_reported_for_pending(store):
    clock = [500.0]
    idx = ix.Indexer(store, embed=fake_embed, now=lambda: clock[0])
    with store.connect() as conn:
        conn.execute("DELETE FROM index_queue")
    item = store.add_item(b"x", kind="document", title="T", format="md").item
    with store.connect() as conn:
        ix.enqueue(conn, item.id, now=clock[0])
    clock[0] += 42
    st = ix.status(store, now=clock[0])
    assert st["pending"] == 1
    assert st["lag_seconds"] == 42
    idx.run_once()
    assert ix.status(store, now=clock[0])["lag_seconds"] == 0


def test_enqueue_missing_catches_up_unqueued_items(store):
    item = store.add_item(b"hello world", kind="document", title="Hi", format="md").item
    assert ix.status(store)["pending"] == 0
    assert ix.enqueue_missing(store) == 1
    assert ix.enqueue_missing(store) == 0
    ix.Indexer(store, embed=fake_embed).run_once()
    assert sr.fulltext(store, "hello", include_private=True)[0].item_id == item.id


def test_pluggable_text_for_and_default_formats(store):
    _add(store, "Scan", b"%PDF-binary", fmt="pdf")
    js = _add(store, "Data", b'{"k": "zebra"}', fmt="json")
    ix.Indexer(store, embed=fake_embed).run_once()
    assert sr.fulltext(store, "zebra")[0].item_id == js.id
    assert [h.item_id for h in sr.fulltext(store, "binary")] == []
    ix.requeue_all(store)
    ix.Indexer(store, embed=fake_embed, text_for=lambda i: "walrus").run_once()
    assert sr.fulltext(store, "walrus")


def test_background_thread_indexes_and_stops(store):
    item = _add(store, "Felines", b"cat body")
    idx = ix.Indexer(store, embed=fake_embed)
    idx.start(poll=0.01)
    try:
        for _ in range(200):
            if ix.status(store)["done"] == 1:
                break
            idx._wake.wait(0.02)
    finally:
        idx.stop()
    assert ix.status(store)["done"] == 1
    assert sr.semantic(store, "cat", fake_embed)[0].item_id == item.id


def test_chunking():
    assert sr.chunk_text("") == []
    chunks = sr.chunk_text("para one\n\n" + ("word " * 600), max_chars=500)
    assert len(chunks) > 3
    assert all(len(c) <= 500 for c in chunks)


def test_vector_blob_roundtrip():
    v = [0.5, -1.0, 2.0]
    assert sr.unpack_vec(sr.pack_vec(v)) == v


# ------------------------------------------------------------------ no embedder (d4 item 1)


def test_indexer_without_embedder_indexes_body_and_completes(store):
    item = _add(store, "Notes", b"the narwhal surfaces at dawn")
    idx = ix.Indexer(store)  # no gateway configured
    assert idx.run_once() == 1
    st = ix.status(store)
    assert st["done"] == 1
    assert st["pending"] == 0
    assert st["failed"] == 0
    assert st["last_error"] is None
    assert st["last_success_at"] is not None
    assert sr.fulltext(store, "narwhal")[0].item_id == item.id
    with store.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0


def test_default_text_for_extracts_epub_and_markdown(store):
    from tests.core.test_ingest import make_epub

    epub = make_epub(
        {"c1.xhtml": "<html><body><h1>Front page</h1><p>Parliament debates okapi</p></body></html>"}
    )
    book = _add(store, "Daily", epub, fmt="epub", kind="newspaper")
    md = _add(store, "Memo", b"# Heading\n\n**bold** quokka", fmt="md")
    ix.Indexer(store).run_once()
    assert [h.item_id for h in sr.fulltext(store, "okapi")] == [book.id]
    assert [h.item_id for h in sr.fulltext(store, "quokka")] == [md.id]
    # extracted text, not raw markup
    with store.reader() as conn:
        body = conn.execute("SELECT body FROM search_fts WHERE rowid=?", (md.id,)).fetchone()[0]
    assert "**" not in body
    assert "#" not in body


def test_body_only_jobs_get_embeddings_once_an_embedder_appears(store):
    item = _add(store, "Felines", b"all about the cat family")
    ix.Indexer(store).run_once()
    assert ix.requeue_unembedded(store) == 1
    ix.Indexer(store, embed=fake_embed).run_once()
    assert sr.semantic(store, "cat", fake_embed)[0].item_id == item.id
    assert ix.requeue_unembedded(store) == 0
