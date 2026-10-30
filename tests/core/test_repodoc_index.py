"""Repo documents in search and RAG (devague-work plan task t11; deviations d7 and d8).

Repo docs are indexed by the background indexer's own discovery (no push-route hook), at a
lower priority than library items, into ``repodoc_fts`` / ``repodoc_embeddings``. Every
search function keeps returning library items only unless the caller passes
``corpora=("item", "repodoc")``.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from culture_library.core import indexer as ix
from culture_library.core import search as sr
from culture_library.core.repodocs import RepoDocs

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

    def __call__(self, texts):
        if not self.up:
            raise ConnectionRefusedError("gateway down")
        return fake_embed(texts)


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


def _add_item(store, title, body, vis="public"):
    res = store.add_item(body, kind="document", title=title, format="md", visibility=vis)
    with store.connect() as conn:
        ix.index_on_add(conn, res.item)
    return res.item


def _count(store, sql, params=()):
    conn = sqlite3.connect(store.db_path)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def _doc_hits(store, q, **kw):
    return [h for h in sr.fulltext(store, q, corpora=BOTH, **kw) if h.corpus == "repodoc"]


# ---------------------------------------------------------------- shape (d8)


def test_hit_and_passage_default_to_item_corpus():
    h = sr.Hit(1, "t", "book", "public", 1.0, "s", "fulltext")
    p = sr.Passage(1, "t", "book", 0, "sha", "x", 1.0, "semantic")
    assert h.corpus == p.corpus == "item"
    assert h.doc_id is None and h.repo is None and h.path is None
    assert p.doc_id is None and p.repo is None and p.path is None


def test_items_hits_carry_corpus_item(store):
    _add_item(store, "Walrus notes", b"walrus")
    hits = sr.fulltext(store, "walrus")
    assert [h.corpus for h in hits] == ["item"]


def test_unknown_corpus_is_rejected(store):
    with pytest.raises(ValueError):
        sr.fulltext(store, "x", corpora=("item", "nope"))


# ---------------------------------------------------------------- text (criterion 1)


def test_markdown_spec_is_found_by_a_body_phrase_only_when_asked(store, docs):
    pushed = _commit(docs, "docs/specs/s.md", "# Lantern spec\n\nThe zeppelin docks at noon.\n")
    ix.Indexer(store).run_once()
    hits = _doc_hits(store, "zeppelin")
    assert len(hits) == 1
    h = hits[0]
    assert (h.corpus, h.doc_id, h.item_id) == ("repodoc", pushed.doc.id, None)
    assert (h.repo, h.path, h.kind) == (pushed.doc.repo, "docs/specs/s.md", "spec")
    assert h.title == "Lantern spec"
    assert h.source == "fulltext"
    assert "[zeppelin]" in h.snippet
    assert sr.fulltext(store, "zeppelin") == []  # default corpora: items only


def test_draft_ahead_is_indexed_instead_of_current(store, docs):
    _commit(docs, "docs/plans/p.md", "old platypus text\n")
    _working(docs, "docs/plans/p.md", "new narwhal text\n")
    ix.Indexer(store).run_once()
    assert _doc_hits(store, "narwhal")
    assert _doc_hits(store, "platypus") == []


def test_draft_equal_to_newest_version_shows_current(store, docs):
    _commit(docs, "docs/plans/p.md", "same okapi text\n")
    _working(docs, "docs/plans/p.md", "same okapi text\n")
    ix.Indexer(store).run_once()
    assert _doc_hits(store, "okapi")


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
    assert "{" not in body and '"claims"' not in body


def test_binary_design_indexes_only_path_and_title(store, docs):
    _commit(docs, "docs/design/mockup-harbor.png", b"\x89PNG\r\n\x1a\nquokka\x00\xff")
    ix.Indexer(store).run_once()
    hits = _doc_hits(store, "harbor")
    assert len(hits) == 1 and hits[0].kind == "design"
    assert _doc_hits(store, "quokka") == []
    assert _count(store, "SELECT body FROM repodoc_fts") == ""


def test_new_push_is_rediscovered_and_reindexed(store, docs):
    idx = ix.Indexer(store)
    _commit(docs, "docs/specs/s.md", "first ibis\n")
    idx.run_once()
    _commit(docs, "docs/specs/s.md", "second heron\n", commit=B, when="2026-10-02T00:00:00Z")
    idx.run_once()
    assert _doc_hits(store, "heron") and _doc_hits(store, "ibis") == []


# ---------------------------------------------------------------- removed docs (d7)


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


def test_removed_doc_is_never_enqueued(store, docs):
    _commit(docs, "docs/specs/s.md", "cormorant\n")
    docs.tombstone(REMOTE, "docs/specs/s.md", B)
    assert ix.discover_repodocs(store) == 0
    assert _count(store, "SELECT count(*) FROM repodoc_index_queue") == 0


# ---------------------------------------------------------------- visibility (criterion 3)


def test_private_repodoc_needs_include_private(store, docs):
    _commit(docs, "docs/specs/s.md", "secret pangolin\n", vis="private")
    ix.Indexer(store, embed=fake_embed).run_once()
    assert _doc_hits(store, "pangolin") == []
    assert _doc_hits(store, "pangolin", owner="any-key") == []  # no uploader-owner link
    hits = _doc_hits(store, "pangolin", include_private=True)
    assert len(hits) == 1 and hits[0].visibility == "private"


def test_public_doc_with_private_draft_ahead_fails_closed(store, docs):
    _commit(docs, "docs/specs/s.md", "public tapir\n")
    _working(docs, "docs/specs/s.md", "private draft tapir\n", vis="private")
    ix.Indexer(store).run_once()
    assert _doc_hits(store, "tapir") == []
    assert _doc_hits(store, "tapir", include_private=True)


def test_semantic_hybrid_and_passages_include_repodocs_only_when_asked(store, docs):
    _add_item(store, "Dog book", b"all about dogs")
    pushed = _commit(docs, "docs/specs/s.md", "# Cat spec\n\nfelines everywhere\n")
    ix.Indexer(store, embed=fake_embed).run_once()

    sem = sr.semantic(store, "cat", fake_embed)
    assert all(h.corpus == "item" for h in sem)
    sem2 = sr.semantic(store, "cat", fake_embed, corpora=BOTH)
    assert sem2[0].corpus == "repodoc" and sem2[0].doc_id == pushed.doc.id

    hyb = sr.hybrid(store, "felines", fake_embed, corpora=BOTH)
    assert hyb[0].corpus == "repodoc" and hyb[0].source == "hybrid"
    assert all(h.corpus == "item" for h in sr.hybrid(store, "felines", fake_embed))

    with store.reader() as conn:
        qvec = fake_embed(["cat"])[0]
        default = sr.passages(conn, qvec, 10, "hybrid", sr.Vis(), query="cat")
        both = sr.passages(conn, qvec, 10, "hybrid", sr.Vis(), query="cat", corpora=BOTH)
        narrowed = sr.passages(conn, qvec, 10, "semantic", sr.Vis(), None, [1], corpora=BOTH)
    assert default and all(p.corpus == "item" for p in default)
    top = both[0]
    assert (top.corpus, top.doc_id, top.item_id, top.path) == (
        "repodoc",
        pushed.doc.id,
        None,
        "docs/specs/s.md",
    )
    assert top.chunk_sha == sr.chunk_sha(top.text)
    assert all(p.corpus == "item" for p in narrowed)  # item_ids narrows to items


def test_private_repodoc_passages_hidden(store, docs):
    _commit(docs, "docs/specs/s.md", "cats\n", vis="private")
    ix.Indexer(store, embed=fake_embed).run_once()
    qvec = fake_embed(["cat"])[0]
    with store.reader() as conn:
        assert sr.passages(conn, qvec, 5, "semantic", sr.Vis(), corpora=BOTH) == []
        assert sr.passages(conn, qvec, 5, "semantic", sr.Vis(True), corpora=BOTH)


# ---------------------------------------------------------------- baseline isolation


def test_item_ranking_unchanged_by_indexed_repodocs(store, docs):
    _add_item(store, "Whale ship", b"whale ship sea captain")
    _add_item(store, "Whales", b"whale whale ocean")
    _add_item(store, "Harbor", b"ship ship ship captain harbor long text and more words")
    ix.Indexer(store, embed=fake_embed).run_once()
    before = [(h.item_id, h.score) for h in sr.fulltext(store, "whale ship")]
    for n in range(30):
        _commit(docs, f"docs/specs/s{n}.md", "whale " * 3 + "filler " * 50)
    ix.Indexer(store, embed=fake_embed).run_once(limit=100)
    assert _count(store, "SELECT count(*) FROM repodoc_fts") == 30
    after = [(h.item_id, h.score) for h in sr.fulltext(store, "whale ship")]
    assert before == after


# ---------------------------------------------------------------- priority + retry (criterion 2)


def test_index_queue_has_priority_column(store):
    conn = sqlite3.connect(store.db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(index_queue)")}
    finally:
        conn.close()
    assert "priority" in cols


def test_priority_column_added_to_an_old_index_queue(tmp_path):
    from culture_library.core import Store

    data = tmp_path / "data"
    Store(data)
    conn = sqlite3.connect(data / "library.db")
    conn.execute("DROP TABLE index_queue")
    conn.execute(
        "CREATE TABLE index_queue (item_id INTEGER PRIMARY KEY REFERENCES items(id) "
        "ON DELETE CASCADE, state TEXT NOT NULL DEFAULT 'pending', body_done INTEGER NOT "
        "NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL "
        "DEFAULT 0, enqueued_at REAL NOT NULL, last_error TEXT)"
    )
    conn.commit()
    conn.close()
    Store(data)
    conn = sqlite3.connect(data / "library.db")
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(index_queue)")}
    finally:
        conn.close()
    assert "priority" in cols


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
    assert row[0] == "pending" and row[1] == 1 and row[2] == 1010.0
    assert "gateway down" in row[3]
    assert _doc_hits(store, "cats")  # full text is written before the embeddings
    emb.up = True
    assert idx.run_once() == 0  # still backing off
    clock[0] += 11
    assert idx.run_once() == 1
    assert _count(store, "SELECT state FROM repodoc_index_queue") == "done"
    assert _count(store, "SELECT count(*) FROM repodoc_embeddings") >= 2


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


def test_requeue_unembedded_reopens_repodocs(store, docs):
    _commit(docs, "docs/specs/s.md", "cats\n")
    ix.Indexer(store).run_once()
    assert ix.repodoc_status(store)["done"] == 1
    assert ix.requeue_unembedded(store) == 1
    ix.Indexer(store, embed=fake_embed).run_once()
    assert _count(store, "SELECT count(*) FROM repodoc_embeddings") >= 2


def test_repodoc_text_rules():
    assert ix.repodoc_text("docs/a.md", b"# Title here\n\nbody\n") == (
        "Title here",
        "# Title here\n\nbody\n",
    )
    title, body = ix.repodoc_text("docs/design/x.png", b"\x89PNG\x00")
    assert (title, body) == ("x.png", "")
    title, body = ix.repodoc_text(".devague/frames/f.json", b"not json {")
    assert title == "f.json" and "not json" in body
