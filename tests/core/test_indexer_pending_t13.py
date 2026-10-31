"""Indexer tests that wait for the indexer's Postgres port (plan task t13).

Moved verbatim from tests/core/test_search.py and tests/core/test_repodoc_index.py by task
t12, which ported search.py but not the indexer: these drive ``Indexer.run_once``, the job
queues, backoff and text extraction, and still use SQLite idioms (``store.db_path``,
``search_fts``/``repodoc_fts``, ``?`` placeholders). Task t13 ports them to Postgres (the
full-text tables are now ``item_text``/``repodoc_text``; index rows can be checked with
``tests/core/search_helpers.py``) and removes the skip.
"""

from __future__ import annotations

import json  # noqa: F401
import sqlite3  # noqa: F401

import pytest

try:  # the indexer does not import on Postgres until t13
    from culture_library.core import indexer as ix
except ImportError:  # pragma: no cover - until t13
    ix = None
from culture_library.core import search as sr  # noqa: F401
from culture_library.core.repodocs import RepoDocs

pytestmark = pytest.mark.skip(reason="pending t13: the indexer's Postgres port")

A = "a" * 40
B = "b" * 40
REMOTE = "https://github.com/agentculture/lobes-cli"
BOTH = ("item", "repodoc")


def fake_embed(texts):
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


def _add(store, title, body=b"", *, vis="public", fmt="md", **kw):
    meta = dict(kind="document", title=title, format=fmt, visibility=vis)
    meta.update(kw)
    res = store.add_item(body or title.encode(), **meta)
    with store.connect() as conn:
        ix.index_on_add(conn, res.item)
    return res.item


_add_item = _add


@pytest.fixture
def docs(store):
    return RepoDocs(store)


def _commit(docs, path, content, *, commit=A, vis="public", when="2026-10-01T00:00:00Z"):
    return docs.push(
        remote=REMOTE,
        path=path,
        commit=commit,
        committed_at=when,
        default_branch_head=commit,
        visibility=vis,
        content=content,
    )


def _working(docs, path, content, *, vis="public"):
    return docs.push(remote=REMOTE, path=path, commit="working", visibility=vis, content=content)


def _count(store, sql, params=()):
    conn = sqlite3.connect(store.db_path)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def _doc_hits(store, q, **kw):
    return [h for h in sr.fulltext(store, q, corpora=BOTH, **kw) if h.corpus == "repodoc"]


# from tests/core/test_search.py (SQLite)
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


# from tests/core/test_search.py (SQLite)
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


# from tests/core/test_search.py (SQLite)
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


# from tests/core/test_search.py (SQLite)
def test_enqueue_missing_catches_up_unqueued_items(store):
    item = store.add_item(b"hello world", kind="document", title="Hi", format="md").item
    assert ix.status(store)["pending"] == 0
    assert ix.enqueue_missing(store) == 1
    assert ix.enqueue_missing(store) == 0
    ix.Indexer(store, embed=fake_embed).run_once()
    assert sr.fulltext(store, "hello", include_private=True)[0].item_id == item.id


# from tests/core/test_search.py (SQLite)
def test_pluggable_text_for_and_default_formats(store):
    _add(store, "Scan", b"%PDF-binary", fmt="pdf")
    js = _add(store, "Data", b'{"k": "zebra"}', fmt="json")
    ix.Indexer(store, embed=fake_embed).run_once()
    assert sr.fulltext(store, "zebra")[0].item_id == js.id
    assert [h.item_id for h in sr.fulltext(store, "binary")] == []
    ix.requeue_all(store)
    ix.Indexer(store, embed=fake_embed, text_for=lambda i: "walrus").run_once()
    assert sr.fulltext(store, "walrus")


# from tests/core/test_search.py (SQLite)
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


# from tests/core/test_search.py (SQLite)
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


# from tests/core/test_search.py (SQLite)
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


# from tests/core/test_search.py (SQLite)
def test_body_only_jobs_get_embeddings_once_an_embedder_appears(store):
    item = _add(store, "Felines", b"all about the cat family")
    ix.Indexer(store).run_once()
    assert ix.requeue_unembedded(store) == 1
    ix.Indexer(store, embed=fake_embed).run_once()
    assert sr.semantic(store, "cat", fake_embed)[0].item_id == item.id
    assert ix.requeue_unembedded(store) == 0


# from tests/core/test_repodoc_index.py (SQLite)
def test_draft_ahead_is_indexed_instead_of_current(store, docs):
    _commit(docs, "docs/plans/p.md", "old platypus text\n")
    _working(docs, "docs/plans/p.md", "new narwhal text\n")
    ix.Indexer(store).run_once()
    assert _doc_hits(store, "narwhal")
    assert _doc_hits(store, "platypus") == []


# from tests/core/test_repodoc_index.py (SQLite)
def test_draft_equal_to_newest_version_shows_current(store, docs):
    _commit(docs, "docs/plans/p.md", "same okapi text\n")
    _working(docs, "docs/plans/p.md", "same okapi text\n")
    ix.Indexer(store).run_once()
    assert _doc_hits(store, "okapi")


# from tests/core/test_repodoc_index.py (SQLite)
def test_devague_json_is_indexed_as_prose(store, docs):
    frame = {
        "slug": "f",
        "claims": [{"id": "c1", "kind": "outcome", "text": "Gondolas carry the mail"}],
    }
    _commit(docs, ".devague/frames/f.json", json.dumps(frame, indent=2))
    ix.Indexer(store).run_once()
    assert _doc_hits(store, "gondolas")
    body = _count(store, "SELECT body FROM repodoc_fts")
    assert "Gondolas carry the mail" in body
    assert "{" not in body
    assert '"claims"' not in body


# from tests/core/test_repodoc_index.py (SQLite)
def test_binary_design_indexes_only_path_and_title(store, docs):
    _commit(docs, "docs/design/mockup-harbor.png", b"\x89PNG\r\n\x1a\nquokka\x00\xff")
    ix.Indexer(store).run_once()
    hits = _doc_hits(store, "harbor")
    assert len(hits) == 1
    assert hits[0].kind == "design"
    assert _doc_hits(store, "quokka") == []
    assert _count(store, "SELECT body FROM repodoc_fts") == ""


# from tests/core/test_repodoc_index.py (SQLite)
def test_new_push_is_rediscovered_and_reindexed(store, docs):
    idx = ix.Indexer(store)
    _commit(docs, "docs/specs/s.md", "first ibis\n")
    idx.run_once()
    _commit(docs, "docs/specs/s.md", "second heron\n", commit=B, when="2026-10-02T00:00:00Z")
    idx.run_once()
    assert _doc_hits(store, "heron")
    assert _doc_hits(store, "ibis") == []


# from tests/core/test_repodoc_index.py (SQLite)
def test_removed_doc_is_dropped_from_the_index(store, docs):
    _commit(docs, "docs/specs/s.md", "cormorant cats\n")
    idx = ix.Indexer(store, embed=fake_embed)
    idx.run_once()
    assert _count(store, "SELECT count(*) FROM repodoc_embeddings") > 0
    docs.tombstone(REMOTE, "docs/specs/s.md", B)
    idx.run_once()
    assert _doc_hits(store, "cormorant") == []
    assert _count(store, "SELECT count(*) FROM repodoc_fts") == 0
    assert _count(store, "SELECT count(*) FROM repodoc_embeddings") == 0
    assert _count(store, "SELECT count(*) FROM repodoc_index_queue") == 0


# from tests/core/test_repodoc_index.py (SQLite)
def test_removed_doc_is_never_enqueued(store, docs):
    _commit(docs, "docs/specs/s.md", "cormorant\n")
    docs.tombstone(REMOTE, "docs/specs/s.md", B)
    assert ix.discover_repodocs(store) == 0
    assert _count(store, "SELECT count(*) FROM repodoc_index_queue") == 0


# from tests/core/test_repodoc_index.py (SQLite)
def test_item_enqueued_later_is_indexed_before_queued_repodocs(store, docs):
    for n in range(3):
        _commit(docs, f"docs/specs/s{n}.md", f"backfill doc {n}\n")
    assert ix.discover_repodocs(store) == 3
    item = _add_item(store, "Fresh upload", b"fresh")
    idx = ix.Indexer(store)
    assert idx.run_once(limit=1) == 1
    assert _count(store, "SELECT state FROM index_queue WHERE item_id=?", (item.id,)) == "done"
    assert _count(store, "SELECT count(*) FROM repodoc_index_queue WHERE state='pending'") == 3
    assert ix.repodoc_status(store)["pending"] == 3


# from tests/core/test_repodoc_index.py (SQLite)
def test_new_item_preempts_the_repodoc_backfill(store, docs, monkeypatch):
    for n in range(3):
        _commit(docs, f"docs/specs/s{n}.md", f"backfill doc {n}\n")
    idx = ix.Indexer(store)
    real = idx._process_doc
    added = []

    def process(doc_id):
        ok = real(doc_id)
        if not added:  # an upload lands while the backfill runs
            added.append(_add_item(store, "Mid upload", b"mid"))
        return ok

    monkeypatch.setattr(idx, "_process_doc", process)
    idx.run_once()
    assert _count(store, "SELECT count(*) FROM repodoc_index_queue WHERE state='done'") == 1
    idx.run_once(limit=1)
    assert _count(store, "SELECT state FROM index_queue WHERE item_id=?", (added[0].id,)) == "done"


# from tests/core/test_repodoc_index.py (SQLite)
def test_gateway_failure_leaves_repodoc_jobs_queued_and_they_resume(store, docs):
    clock = [1000.0]
    emb = DownEmbed()
    idx = ix.Indexer(store, embed=emb, now=lambda: clock[0], backoff_base=10, backoff_cap=100)
    _commit(docs, "docs/specs/s.md", "cats in the spec\n")
    assert idx.run_once() == 0
    row = (
        sqlite3.connect(store.db_path)
        .execute("SELECT state, attempts, next_attempt_at, last_error FROM repodoc_index_queue")
        .fetchone()
    )
    assert row[0] == "pending"
    assert row[1] == 1
    assert row[2] == 1010.0
    assert "gateway down" in row[3]
    assert _doc_hits(store, "cats")  # full text is written before the embeddings
    emb.up = True
    assert idx.run_once() == 0  # still backing off
    clock[0] += 11
    assert idx.run_once() == 1
    assert _count(store, "SELECT state FROM repodoc_index_queue") == "done"
    assert _count(store, "SELECT count(*) FROM repodoc_embeddings") >= 2


# from tests/core/test_repodoc_index.py (SQLite)
def test_repodoc_jobs_give_up_and_requeue_failed(store, docs):
    clock = [0.0]
    idx = ix.Indexer(store, embed=DownEmbed(), now=lambda: clock[0], max_attempts=2, backoff_base=1)
    _commit(docs, "docs/specs/s.md", "x\n")
    for _ in range(3):
        clock[0] += 10
        idx.run_once()
    assert ix.repodoc_status(store)["failed"] == 1
    assert ix.requeue_failed(store) == 1
    assert ix.repodoc_status(store)["pending"] == 1


# from tests/core/test_repodoc_index.py (SQLite)
def test_requeue_unembedded_reopens_repodocs(store, docs):
    _commit(docs, "docs/specs/s.md", "cats\n")
    ix.Indexer(store).run_once()
    assert ix.repodoc_status(store)["done"] == 1
    assert ix.requeue_unembedded(store) == 1
    ix.Indexer(store, embed=fake_embed).run_once()
    assert _count(store, "SELECT count(*) FROM repodoc_embeddings") >= 2


# from tests/core/test_repodoc_index.py (SQLite)
def test_repodoc_text_rules():
    assert ix.repodoc_text("docs/a.md", b"# Title here\n\nbody\n") == (
        "Title here",
        "# Title here\n\nbody\n",
    )
    title, body = ix.repodoc_text("docs/design/x.png", b"\x89PNG\x00")
    assert (title, body) == ("x.png", "")
    title, body = ix.repodoc_text(".devague/frames/f.json", b"not json {")
    assert title == "f.json"
    assert "not json" in body
