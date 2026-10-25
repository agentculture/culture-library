"""Chunk-level passages for RAG (t1). No network: embeddings are written directly or by a
stub embedder, and the query vector is passed in (core never embeds a query here)."""

import hashlib

import pytest

from culture_library.core import indexer as ix
from culture_library.core import search as sr
from culture_library.core import uploaders

PUBLIC = sr.Vis()
PRIVATE = sr.Vis(include_private=True)


def fake_embed(texts):
    out = []
    for t in texts:
        low = t.lower()
        out.append([float("cat" in low), float("dog" in low), 0.1])
    return out


def _add(store, title, body, *, vis="public", kind="document", owner=None):
    res = store.add_item(
        body.encode(), kind=kind, title=title, format="md", visibility=vis, author=""
    )
    with store.connect() as conn:
        ix.index_on_add(conn, res.item)
        if owner:
            uploaders.record(conn, res.item.id, owner)
    return res.item


def _index(store):
    ix.Indexer(store, embed=fake_embed).run_once()


CAT_BODY = "cats purr and nap all day\n\nsome filler about weather"
DOG_BODY = "dogs bark at the mailman\n\nmore dog things"


@pytest.fixture
def lib(store):
    cat = _add(store, "Felines", CAT_BODY)
    dog = _add(store, "Canines", DOG_BODY, kind="article")
    _index(store)
    return store, cat, dog


def test_semantic_returns_full_chunk_text_ranked_by_cosine(lib):
    store, cat, dog = lib
    with store.reader() as conn:
        ps = sr.passages(conn, [1.0, 0.0, 0.0], 10, "semantic", PUBLIC)
    assert ps, "expected passages"
    top = ps[0]
    assert isinstance(top, sr.Passage)
    assert top.item_id == cat.id
    assert top.title == "Felines"
    assert top.kind == "document"
    assert top.source == "semantic"
    # full stored chunk text, not a snippet
    with store.reader() as conn:
        stored = conn.execute(
            "SELECT text FROM embeddings WHERE item_id=? AND chunk_no=?",
            (top.item_id, top.chunk_no),
        ).fetchone()[0]
    assert top.text == stored
    assert top.score == pytest.approx(
        sr._cosine([1.0, 0.0, 0.0], fake_embed([stored])[0]), rel=1e-5
    )
    scores = [p.score for p in ps]
    assert scores == sorted(scores, reverse=True)
    # chunk-level: more than one passage per item can come back
    assert len({(p.item_id, p.chunk_no) for p in ps}) == len(ps)
    assert sum(p.item_id == dog.id for p in ps) >= 2


def test_long_chunk_text_is_not_truncated(store):
    body = "cat " * 240  # ~960 chars, one chunk, far beyond the 200-char Hit snippet
    item = _add(store, "Long", body)
    _index(store)
    with store.reader() as conn:
        ps = sr.passages(conn, [1.0, 0.0, 0.0], 5, "semantic", PUBLIC)
    body_p = [p for p in ps if p.item_id == item.id and not p.is_header][0]
    assert body_p.text == body.strip()
    assert len(body_p.text) > 200


def test_header_chunk_is_flagged(lib):
    store, cat, _ = lib
    with store.reader() as conn:
        ps = sr.passages(conn, [0.0, 0.0, 1.0], 50, "semantic", PUBLIC)
    headers = [p for p in ps if p.is_header]
    assert headers
    assert all(p.chunk_no == 0 for p in headers)
    assert all(not p.is_header for p in ps if p.chunk_no != 0)
    assert any(p.item_id == cat.id and p.text == "Felines" for p in headers)


def test_chunk_sha_is_sha256_of_text_and_stable_across_reindex(lib):
    store, cat, _ = lib
    with store.reader() as conn:
        before = sr.passages(conn, [1.0, 0.0, 0.0], 50, "semantic", PUBLIC)
    for p in before:
        assert p.chunk_sha == hashlib.sha256(p.text.encode("utf-8")).hexdigest()
        assert p.chunk_sha == sr.chunk_sha(p.text)
    # re-index: rewrite the embeddings from the same chunks
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT chunk_no, text FROM embeddings WHERE item_id=? ORDER BY chunk_no", (cat.id,)
        ).fetchall()
        texts = [r[1] for r in rows]
        sr.set_embeddings(conn, cat.id, texts, fake_embed(texts), "m2")
    with store.reader() as conn:
        after = sr.passages(conn, [1.0, 0.0, 0.0], 50, "semantic", PUBLIC)
    key = lambda ps: {(p.item_id, p.chunk_no): p.chunk_sha for p in ps}  # noqa: E731
    assert key(before) == key(after)


def test_hybrid_fuses_semantic_chunk_rank_with_fts_item_rank(store):
    # vector says "cat" chunk of A wins; FTS says only B matches "mailman"
    a = _add(store, "Alpha", "cats everywhere")
    b = _add(store, "Beta", "the mailman came by")
    _index(store)
    with store.reader() as conn:
        sem = sr.passages(conn, [1.0, 0.0, 0.0], 50, "semantic", PUBLIC)
        hyb = sr.passages(conn, [1.0, 0.0, 0.0], 50, "hybrid", PUBLIC, query="mailman")
    assert all(p.source == "hybrid" for p in hyb)
    sem_pos = {(p.item_id, p.chunk_no): i for i, p in enumerate(sem)}
    for p in hyb:
        expected = 1.0 / (60 + sem_pos[(p.item_id, p.chunk_no)])
        if p.item_id == b.id:  # FTS item rank 0
            expected += 1.0 / 60
        assert p.score == pytest.approx(expected)
    assert hyb[0].item_id == b.id  # the FTS boost lifts B above A's best chunk
    assert sem[0].item_id == a.id


def test_hybrid_without_fts_match_is_semantic_order(lib):
    store, *_ = lib
    with store.reader() as conn:
        sem = sr.passages(conn, [1.0, 0.0, 0.0], 50, "semantic", PUBLIC)
        hyb = sr.passages(conn, [1.0, 0.0, 0.0], 50, "hybrid", PUBLIC, query="")
    assert [(p.item_id, p.chunk_no) for p in hyb] == [(p.item_id, p.chunk_no) for p in sem]


def test_k_cutoff(lib):
    store, *_ = lib
    with store.reader() as conn:
        assert len(sr.passages(conn, [1.0, 0.0, 0.0], 2, "semantic", PUBLIC)) == 2
        assert sr.passages(conn, [1.0, 0.0, 0.0], 0, "semantic", PUBLIC) == []


def test_unknown_mode_rejected(lib):
    store, *_ = lib
    with store.reader() as conn, pytest.raises(ValueError):
        sr.passages(conn, [1.0, 0.0, 0.0], 5, "fulltext", PUBLIC)


def test_private_hidden_unless_access_or_owner(store):
    pub = _add(store, "Pub cat", "cats in public")
    sec = _add(store, "Sec cat", "cats in secret", vis="private", owner="key-1")
    _index(store)
    q = [1.0, 0.0, 0.0]
    with store.reader() as conn:
        for mode in ("semantic", "hybrid"):
            ids = {p.item_id for p in sr.passages(conn, q, 50, mode, PUBLIC, query="cats")}
            assert ids == {pub.id}
            ids = {p.item_id for p in sr.passages(conn, q, 50, mode, PRIVATE, query="cats")}
            assert ids == {pub.id, sec.id}
            owner = sr.Vis(owner="key-1")
            ids = {p.item_id for p in sr.passages(conn, q, 50, mode, owner, query="cats")}
            assert ids == {pub.id, sec.id}
            other = sr.Vis(owner="key-2")
            ids = {p.item_id for p in sr.passages(conn, q, 50, mode, other, query="cats")}
            assert ids == {pub.id}


def test_visibility_parity_with_search(store):
    """An item search hides for a principal never yields a passage for that principal."""
    _add(store, "Pub cat", "cats in public")
    _add(store, "Sec cat", "cats in secret", vis="private", owner="key-1")
    _add(store, "Other cat", "cats elsewhere", vis="private", owner="key-2")
    _index(store)
    for vis in (PUBLIC, PRIVATE, sr.Vis(owner="key-1"), sr.Vis(owner="key-2")):
        kw = {"include_private": vis.include_private, "owner": vis.owner}
        visible = {h.item_id for h in sr.fulltext(store, "cats", **kw, limit=100)}
        visible |= {h.item_id for h in sr.semantic(store, "cats", fake_embed, **kw, limit=100)}
        with store.reader() as conn:
            for mode in ("semantic", "hybrid"):
                ps = sr.passages(conn, [1.0, 0.0, 0.0], 100, mode, vis, query="cats")
                assert {p.item_id for p in ps} <= visible


def test_vis_accepts_plain_tuple_and_mapping(store):
    _add(store, "Sec cat", "cats in secret", vis="private")
    _index(store)
    with store.reader() as conn:
        assert sr.passages(conn, [1.0, 0, 0], 5, "semantic", (True, None))
        assert sr.passages(conn, [1.0, 0, 0], 5, "semantic", {"include_private": True})
        assert sr.passages(conn, [1.0, 0, 0], 5, "semantic", {}) == []


def test_archived_never_yields(store):
    item = _add(store, "Old cat", "cats of yore")
    _index(store)
    with store.connect() as conn:
        conn.execute("UPDATE items SET archived_at='2026-01-01' WHERE id=?", (item.id,))
    with store.reader() as conn:
        for mode in ("semantic", "hybrid"):
            assert sr.passages(conn, [1.0, 0, 0], 10, mode, PRIVATE, query="cats") == []
        assert sr.item_chunks(conn, item.id, PRIVATE) == []


def test_kind_and_item_ids_filter_before_cutoff(store):
    # many strong cat chunks in a document; one weak dog-ish article chunk
    _add(store, "Cats", "\n\n".join(["cat " * 200] * 4))
    art = _add(store, "Dogs", "dogs only", kind="article")
    _index(store)
    q = [1.0, 0.0, 0.0]
    with store.reader() as conn:
        top1 = sr.passages(conn, q, 1, "semantic", PUBLIC)
        assert top1[0].kind == "document"
        only_art = sr.passages(conn, q, 1, "semantic", PUBLIC, kind="article")
        assert len(only_art) == 1
        assert only_art[0].item_id == art.id
        only_id = sr.passages(conn, q, 1, "hybrid", PUBLIC, item_ids=[art.id], query="cat")
        assert len(only_id) == 1
        assert only_id[0].item_id == art.id
        assert sr.passages(conn, q, 5, "semantic", PUBLIC, item_ids=[]) == []
        assert sr.passages(conn, q, 5, "semantic", PUBLIC, kind="book") == []


def test_item_chunks_lists_in_order_and_respects_visibility(store):
    pub = _add(store, "Felines", CAT_BODY)
    sec = _add(store, "Sec", "cats in secret", vis="private", owner="key-1")
    _index(store)
    with store.reader() as conn:
        chunks = sr.item_chunks(conn, pub.id, PUBLIC)
        assert [c.chunk_no for c in chunks] == list(range(len(chunks)))
        assert chunks[0].is_header
        assert chunks[0].text == "Felines"
        assert not any(c.is_header for c in chunks[1:])
        for c in chunks:
            assert c.chunk_sha == sr.chunk_sha(c.text)
        chunk_no, sha, text, is_header = chunks[1]  # plain-tuple shape
        assert text == CAT_BODY
        assert sr.item_chunks(conn, sec.id, PUBLIC) == []
        assert sr.item_chunks(conn, sec.id, sr.Vis(owner="key-1"))
        assert sr.item_chunks(conn, 99999, PRIVATE) == []
