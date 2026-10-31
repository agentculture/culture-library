"""Repo documents in search and RAG (devague-work plan task t11; deviations d7 and d8), on
Postgres full text and pgvector.

Repo docs live in ``repodoc_text`` / ``repodoc_embeddings``; every search function keeps
returning library items only unless the caller passes ``corpora=("item", "repodoc")``. The
index rows are written directly here; how the indexer discovers, prioritises and retries repo
doc jobs, and which text it shows, is tested with its port (plan task t13,
``tests/core/test_indexer_pending_t13.py``).
"""

from __future__ import annotations

import pytest

from culture_library.core import search as sr
from culture_library.core.repodocs import RepoDocs
from tests.core.search_helpers import add, count, fake_embed, index, index_doc

A = "a" * 40
B = "b" * 40
REMOTE = "https://github.com/agentculture/lobes-cli"
BOTH = ("item", "repodoc")


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


def _doc(store, docs, path, title, body, *, vis="public", text_vis=None, embed=fake_embed):
    pushed = _commit(docs, path, body, vis=vis)
    index_doc(store, pushed.doc, title, body, visibility=text_vis, embed=embed)
    return pushed.doc


def _add_item(store, title, body, vis="public"):
    item = add(store, title, body, vis=vis)
    index(store, item)
    return item


def _doc_hits(store, q, **kw):
    return [h for h in sr.fulltext(store, q, corpora=BOTH, **kw) if h.corpus == "repodoc"]


# ---------------------------------------------------------------- shape (d8)


def test_hit_and_passage_default_to_item_corpus():
    h = sr.Hit(1, "t", "book", "public", 1.0, "s", "fulltext")
    p = sr.Passage(1, "t", "book", 0, "sha", "x", 1.0, "semantic")
    assert h.corpus == p.corpus == "item"
    assert h.doc_id is None
    assert h.repo is None
    assert h.path is None
    assert p.doc_id is None
    assert p.repo is None
    assert p.path is None


def test_items_hits_carry_corpus_item(store):
    _add_item(store, "Walrus notes", "walrus")
    hits = sr.fulltext(store, "walrus")
    assert [h.corpus for h in hits] == ["item"]


def test_unknown_corpus_is_rejected(store):
    with pytest.raises(ValueError):
        sr.fulltext(store, "x", corpora=("item", "nope"))
    with store.reader() as conn, pytest.raises(ValueError):
        sr.passages(conn, fake_embed(["cat"])[0], 5, "semantic", sr.Vis(), corpora=("nope",))


# ---------------------------------------------------------------- text (criterion 1)


def test_markdown_spec_is_found_by_a_body_phrase_only_when_asked(store, docs):
    doc = _doc(
        store, docs, "docs/specs/s.md", "Lantern spec", "# Lantern spec\n\nThe zeppelin docks.\n"
    )
    hits = _doc_hits(store, "zeppelin")
    assert len(hits) == 1
    h = hits[0]
    assert (h.corpus, h.doc_id, h.item_id) == ("repodoc", doc.id, None)
    assert (h.repo, h.path, h.kind) == (doc.repo, "docs/specs/s.md", "spec")
    assert h.title == "Lantern spec"
    assert h.source == "fulltext"
    assert "[zeppelin]" in h.snippet
    assert sr.fulltext(store, "zeppelin") == []  # default corpora: items only


def test_path_is_searchable(store, docs):
    _doc(store, docs, "docs/design/mockup-harbor.png", "mockup-harbor.png", "")
    hits = _doc_hits(store, "harbor")
    assert len(hits) == 1
    assert hits[0].kind == "design"


def test_rewriting_the_text_replaces_it(store, docs):
    doc = _doc(store, docs, "docs/specs/s.md", "S", "first ibis\n")
    index_doc(store, doc, "S", "second heron\n")
    assert _doc_hits(store, "heron")
    assert _doc_hits(store, "ibis") == []
    assert count(store, "SELECT count(*) FROM repodoc_embeddings WHERE doc_id = %s", (doc.id,)) == 2


def test_items_and_docs_merge_by_score_with_ties_on_corpus_then_id(store, docs):
    item = _add_item(store, "Okapi", "okapi")
    doc = _doc(store, docs, "docs/specs/okapi.md", "Okapi", "okapi")
    hits = sr.fulltext(store, "okapi", corpora=BOTH)
    assert {(h.corpus, h.item_id or h.doc_id) for h in hits} == {
        ("item", item.id),
        ("repodoc", doc.id),
    }
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    if scores[0] == scores[1]:
        assert [h.corpus for h in hits] == ["item", "repodoc"]


# ---------------------------------------------------------------- removed docs (d7)


def test_drop_repodoc_removes_text_and_every_vector(store, docs):
    doc = _doc(store, docs, "docs/specs/s.md", "S", "cormorant cats\n")
    assert count(store, "SELECT count(*) FROM repodoc_embeddings") > 0
    with store.connect() as conn:
        sr.drop_repodoc(conn, doc.id)
    assert _doc_hits(store, "cormorant") == []
    assert count(store, "SELECT count(*) FROM repodoc_text") == 0
    assert count(store, "SELECT count(*) FROM repodoc_embeddings") == 0


def test_tombstoned_doc_is_hidden_even_before_its_rows_are_dropped(store, docs):
    _doc(store, docs, "docs/specs/s.md", "S", "cormorant cats\n")
    docs.tombstone(REMOTE, "docs/specs/s.md", B)
    qvec = fake_embed(["cat"])[0]
    assert _doc_hits(store, "cormorant", include_private=True) == []
    sem = sr.semantic(store, "cat", fake_embed, include_private=True, corpora=BOTH)
    assert sem == []
    with store.reader() as conn:
        assert sr.passages(conn, qvec, 5, "hybrid", sr.Vis(True), query="cats", corpora=BOTH) == []


# ---------------------------------------------------------------- visibility (criterion 3)


def test_private_repodoc_needs_include_private(store, docs):
    _doc(store, docs, "docs/specs/s.md", "S", "secret pangolin\n", vis="private")
    assert _doc_hits(store, "pangolin") == []
    assert _doc_hits(store, "pangolin", owner="any-key") == []  # no uploader-owner link
    hits = _doc_hits(store, "pangolin", include_private=True)
    assert len(hits) == 1
    assert hits[0].visibility == "private"


def test_public_doc_with_private_indexed_text_fails_closed(store, docs):
    doc = _doc(store, docs, "docs/specs/s.md", "S", "private draft tapir\n", text_vis="private")
    assert _doc_hits(store, "tapir") == []
    assert sr.semantic(store, "tapir", fake_embed, corpora=BOTH) == []
    assert _doc_hits(store, "tapir", include_private=True)
    with store.connect() as conn:
        sr.set_repodoc_visibility(conn, doc.id, "public")
    assert _doc_hits(store, "tapir")


def test_semantic_hybrid_and_passages_include_repodocs_only_when_asked(store, docs):
    _add_item(store, "Dog book", "all about dogs")
    doc = _doc(store, docs, "docs/specs/s.md", "Cat spec", "# Cat spec\n\nfelines everywhere\n")

    sem = sr.semantic(store, "cat", fake_embed)
    assert all(h.corpus == "item" for h in sem)
    sem2 = sr.semantic(store, "cat", fake_embed, corpora=BOTH)
    assert sem2[0].corpus == "repodoc"
    assert sem2[0].doc_id == doc.id
    assert (sem2[0].repo, sem2[0].path) == (doc.repo, doc.path)

    hyb = sr.hybrid(store, "felines", fake_embed, corpora=BOTH)
    assert hyb[0].corpus == "repodoc"
    assert hyb[0].source == "hybrid"
    assert all(h.corpus == "item" for h in sr.hybrid(store, "felines", fake_embed))

    with store.reader() as conn:
        qvec = fake_embed(["cat"])[0]
        default = sr.passages(conn, qvec, 10, "hybrid", sr.Vis(), query="cat")
        both = sr.passages(conn, qvec, 10, "hybrid", sr.Vis(), query="cat", corpora=BOTH)
        narrowed = sr.passages(conn, qvec, 10, "semantic", sr.Vis(), None, [1], corpora=BOTH)
        kinds = sr.passages(conn, qvec, 10, "semantic", sr.Vis(), "spec", corpora=BOTH)
    assert default
    assert all(p.corpus == "item" for p in default)
    top = both[0]
    assert (top.corpus, top.doc_id, top.item_id, top.path) == (
        "repodoc",
        doc.id,
        None,
        "docs/specs/s.md",
    )
    assert top.chunk_sha == sr.chunk_sha(top.text)
    assert all(p.corpus == "item" for p in narrowed)  # item_ids narrows to items
    assert kinds
    assert {p.corpus for p in kinds} == {"repodoc"}


def test_private_repodoc_passages_hidden(store, docs):
    _doc(store, docs, "docs/specs/s.md", "S", "cats\n", vis="private")
    qvec = fake_embed(["cat"])[0]
    with store.reader() as conn:
        assert sr.passages(conn, qvec, 5, "semantic", sr.Vis(), corpora=BOTH) == []
        assert sr.passages(conn, qvec, 5, "semantic", sr.Vis(True), corpora=BOTH)


def test_repodoc_chunks_in_order_and_visibility(store, docs):
    pub = _doc(store, docs, "docs/specs/a.md", "A", "cats one\n\ncats two\n")
    sec = _doc(store, docs, "docs/specs/b.md", "B", "cats\n", vis="private")
    with store.reader() as conn:
        chunks = sr.repodoc_chunks(conn, pub.id, sr.Vis())
        assert [c.chunk_no for c in chunks] == list(range(len(chunks)))
        assert chunks[0].is_header
        assert chunks[0].text == "A\ndocs/specs/a.md"
        assert sr.repodoc_chunks(conn, sec.id, sr.Vis()) == []
        assert sr.repodoc_chunks(conn, sec.id, sr.Vis(True))


# ---------------------------------------------------------------- baseline isolation


def test_item_ranking_unchanged_by_indexed_repodocs(store, docs):
    """Repo docs never move item rankings: ts_rank has no corpus-wide statistics, and the
    two corpora are separate tables (once a byte-identical bm25 check on SQLite)."""
    _add_item(store, "Whale ship", "whale ship sea captain")
    _add_item(store, "Whales", "whale whale ocean ship")
    _add_item(store, "Harbor", "ship ship ship captain harbor long text whale and more words")
    before = sr.fulltext(store, "whale ship")
    assert len(before) == 3
    for n in range(30):
        _doc(store, docs, f"docs/specs/s{n}.md", f"S{n}", "whale ship " * 3 + "filler " * 50)
    assert count(store, "SELECT count(*) FROM repodoc_text") == 30
    after = sr.fulltext(store, "whale ship")
    assert [h.item_id for h in after] == [h.item_id for h in before]
    assert [h.score for h in after] == pytest.approx([h.score for h in before])
    assert before[0].title == "Whale ship"  # the title match ranks first
