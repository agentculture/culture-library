"""Chunk-level passages for RAG. No network: embeddings are written directly with a stub
embedder, and the query vector is passed in (core never embeds a query here)."""

import hashlib

import pytest

from culture_library.core import embedding_strategy as es
from culture_library.core import search as sr
from tests.core.search_helpers import PAIR, add, fake_embed, index, vec

PUBLIC = sr.Vis()
PRIVATE = sr.Vis(include_private=True)
CAT_Q = vec(1.0, 0.0, 0.0)


def _add(store, title, body, *, vis="public", kind="document", owner=None):
    item = add(store, title, body, vis=vis, kind=kind, owner=owner)
    index(store, item)
    return item


CAT_BODY = "cats purr and nap all day\n\nsome filler about weather"
DOG_BODY = "dogs bark at the mailman\n\nmore dog things"


@pytest.fixture
def lib(store):
    cat = _add(store, "Felines", CAT_BODY)
    dog = _add(store, "Canines", DOG_BODY, kind="article")
    return store, cat, dog


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    return dot / ((sum(x * x for x in a) ** 0.5) * (sum(y * y for y in b) ** 0.5))


def test_semantic_returns_full_chunk_text_ranked_by_cosine(lib):
    store, cat, dog = lib
    with store.reader() as conn:
        ps = sr.passages(conn, CAT_Q, 10, "semantic", PUBLIC)
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
            "SELECT text FROM embeddings WHERE item_id=%s AND chunk_no=%s",
            (top.item_id, top.chunk_no),
        ).fetchone()[0]
    assert top.text == stored
    assert top.score == pytest.approx(_cosine(CAT_Q, fake_embed([stored])[0]), rel=1e-5)
    scores = [p.score for p in ps]
    assert scores == sorted(scores, reverse=True)
    # chunk-level: more than one passage per item can come back
    assert len({(p.item_id, p.chunk_no) for p in ps}) == len(ps)
    assert sum(p.item_id == dog.id for p in ps) >= 2


def test_long_chunk_text_is_not_truncated(store):
    body = "cat " * 240  # ~960 chars, one chunk, far beyond the 200-char Hit snippet
    item = _add(store, "Long", body)
    with store.reader() as conn:
        ps = sr.passages(conn, CAT_Q, 5, "semantic", PUBLIC)
    body_p = [p for p in ps if p.item_id == item.id and not p.is_header][0]
    assert body_p.text == body.strip()
    assert len(body_p.text) > 200


def test_header_chunk_is_flagged(lib):
    store, cat, _ = lib
    with store.reader() as conn:
        ps = sr.passages(conn, vec(0.0, 0.0, 1.0), 50, "semantic", PUBLIC)
    headers = [p for p in ps if p.is_header]
    assert headers
    assert all(p.chunk_no == 0 for p in headers)
    assert all(not p.is_header for p in ps if p.chunk_no != 0)
    assert any(p.item_id == cat.id and p.text == "Felines" for p in headers)


def test_chunk_sha_is_sha256_of_text_and_stable_across_reindex(lib):
    store, cat, _ = lib
    with store.reader() as conn:
        before = sr.passages(conn, CAT_Q, 50, "semantic", PUBLIC)
    for p in before:
        assert p.chunk_sha == hashlib.sha256(p.text.encode("utf-8")).hexdigest()
        assert p.chunk_sha == sr.chunk_sha(p.text)
    # re-index: rewrite the embeddings from the same chunks
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT chunk_no, text FROM embeddings WHERE item_id=%s ORDER BY chunk_no", (cat.id,)
        ).fetchall()
        texts = [r[1] for r in rows]
        sr.set_embeddings(conn, cat.id, texts, fake_embed(texts), **PAIR)
    with store.reader() as conn:
        after = sr.passages(conn, CAT_Q, 50, "semantic", PUBLIC)
    key = lambda ps: {(p.item_id, p.chunk_no): p.chunk_sha for p in ps}  # noqa: E731
    assert key(before) == key(after)


def test_hybrid_fuses_semantic_chunk_rank_with_fts_item_rank(store):
    # vector says "cat" chunk of A wins; full text says only B matches "mailman"
    a = _add(store, "Alpha", "cats everywhere")
    b = _add(store, "Beta", "the mailman came by")
    with store.reader() as conn:
        sem = sr.passages(conn, CAT_Q, 50, "semantic", PUBLIC)
        hyb = sr.passages(conn, CAT_Q, 50, "hybrid", PUBLIC, query="mailman")
    assert all(p.source == "hybrid" for p in hyb)
    sem_pos = {(p.item_id, p.chunk_no): i for i, p in enumerate(sem)}
    for p in hyb:
        expected = 1.0 / (60 + sem_pos[(p.item_id, p.chunk_no)])
        if p.item_id == b.id:  # full-text item rank 0
            expected += 1.0 / 60
        assert p.score == pytest.approx(expected)
    assert hyb[0].item_id == b.id  # the full-text boost lifts B above A's best chunk
    assert sem[0].item_id == a.id


def test_hybrid_reaches_a_fulltext_match_outside_the_semantic_pool(store):
    """An item full text ranks first gets its boost even when none of its chunks is among
    the nearest ones (the pool adds the full-text owners' own nearest chunks)."""
    many = [_add(store, f"Cat {n}", f"cat story {n}") for n in range(30)]
    far = add(store, "Mailbag", "the mailman came by")
    with store.connect() as conn:
        sr.set_body(conn, far, "the mailman came by")
        sr.set_embeddings(conn, far.id, ["Mailbag", "mailman"], [vec(0, 1.0), vec(0, 1.0)], **PAIR)
    with store.reader() as conn:
        sem = sr.passages(conn, CAT_Q, 2, "semantic", PUBLIC)
        hyb = sr.passages(conn, CAT_Q, 2, "hybrid", PUBLIC, query="mailman")
    assert far.id not in {p.item_id for p in sem}
    assert hyb[0].item_id == far.id
    assert {p.item_id for p in sem} <= {m.id for m in many}


def test_hybrid_without_fts_match_is_semantic_order(lib):
    store, *_ = lib
    with store.reader() as conn:
        sem = sr.passages(conn, CAT_Q, 50, "semantic", PUBLIC)
        hyb = sr.passages(conn, CAT_Q, 50, "hybrid", PUBLIC, query="")
    assert [(p.item_id, p.chunk_no) for p in hyb] == [(p.item_id, p.chunk_no) for p in sem]


def test_ties_break_on_id_then_chunk_no(store):
    a = add(store, "A", "a")
    b = add(store, "B", "b")
    with store.connect() as conn:
        for item in (b, a):
            sr.set_embeddings(conn, item.id, ["x", "y"], [vec(1.0), vec(1.0)], **PAIR)
    with store.reader() as conn:
        for mode in ("semantic", "hybrid"):
            ps = sr.passages(conn, CAT_Q, 4, mode, PUBLIC)
            assert [(p.item_id, p.chunk_no) for p in ps] == [
                (a.id, 0),
                (a.id, 1),
                (b.id, 0),
                (b.id, 1),
            ]


def test_k_cutoff(lib):
    store, *_ = lib
    with store.reader() as conn:
        assert len(sr.passages(conn, CAT_Q, 2, "semantic", PUBLIC)) == 2
        assert len(sr.passages(conn, CAT_Q, 2, "hybrid", PUBLIC, query="cats")) == 2
        assert sr.passages(conn, CAT_Q, 0, "semantic", PUBLIC) == []


def test_unknown_mode_rejected(lib):
    store, *_ = lib
    with store.reader() as conn, pytest.raises(ValueError):
        sr.passages(conn, CAT_Q, 5, "fulltext", PUBLIC)


def test_wrong_query_dimension_rejected(lib):
    store, *_ = lib
    with store.reader() as conn, pytest.raises(ValueError):
        sr.passages(conn, [1.0, 0.0, 0.0], 5, "semantic", PUBLIC)


def test_another_strategy_is_never_scored(store):
    other = es.Strategy("d", "q", "paragraph-pack", 1000, 3, "l2")
    a = add(store, "A", "cats a")
    b = add(store, "B", "cats b")
    with store.connect() as conn:
        sr.set_embeddings(conn, a.id, ["x"], [vec(1.0)], **PAIR)
        sr.set_embeddings(conn, b.id, ["x"], [[1.0, 0, 0]], model_id="m3", strategy=other)
    with store.reader() as conn:
        for mode in ("semantic", "hybrid"):
            ps = sr.passages(conn, CAT_Q, 10, mode, PUBLIC, query="cats")
            assert {p.item_id for p in ps} == {a.id}
            ps = sr.passages(
                conn, [1.0, 0, 0], 10, mode, PUBLIC, query="cats", model_id="m3", strategy=other
            )
            assert {p.item_id for p in ps} == {b.id}
        assert sr.item_chunks(conn, b.id, PUBLIC) == []
        assert len(sr.item_chunks(conn, b.id, PUBLIC, model_id="m3", strategy=other)) == 1


def test_private_hidden_unless_access_or_owner(store):
    pub = _add(store, "Pub cat", "cats in public")
    sec = _add(store, "Sec cat", "cats in secret", vis="private", owner="key-1")
    with store.reader() as conn:
        for mode in ("semantic", "hybrid"):
            ids = {p.item_id for p in sr.passages(conn, CAT_Q, 50, mode, PUBLIC, query="cats")}
            assert ids == {pub.id}
            ids = {p.item_id for p in sr.passages(conn, CAT_Q, 50, mode, PRIVATE, query="cats")}
            assert ids == {pub.id, sec.id}
            owner = sr.Vis(owner="key-1")
            ids = {p.item_id for p in sr.passages(conn, CAT_Q, 50, mode, owner, query="cats")}
            assert ids == {pub.id, sec.id}
            other = sr.Vis(owner="key-2")
            ids = {p.item_id for p in sr.passages(conn, CAT_Q, 50, mode, other, query="cats")}
            assert ids == {pub.id}


def test_visibility_parity_with_search(store):
    """An item search hides for a principal never yields a passage for that principal."""
    _add(store, "Pub cat", "cats in public")
    _add(store, "Sec cat", "cats in secret", vis="private", owner="key-1")
    _add(store, "Other cat", "cats elsewhere", vis="private", owner="key-2")
    for vis in (PUBLIC, PRIVATE, sr.Vis(owner="key-1"), sr.Vis(owner="key-2")):
        kw = {"include_private": vis.include_private, "owner": vis.owner}
        visible = {h.item_id for h in sr.fulltext(store, "cats", **kw, limit=100)}
        visible |= {h.item_id for h in sr.semantic(store, "cats", fake_embed, **kw, limit=100)}
        with store.reader() as conn:
            for mode in ("semantic", "hybrid"):
                ps = sr.passages(conn, CAT_Q, 100, mode, vis, query="cats")
                assert {p.item_id for p in ps} <= visible


def test_vis_accepts_plain_tuple_and_mapping(store):
    _add(store, "Sec cat", "cats in secret", vis="private")
    with store.reader() as conn:
        assert sr.passages(conn, CAT_Q, 5, "semantic", (True, None))
        assert sr.passages(conn, CAT_Q, 5, "semantic", {"include_private": True})
        assert sr.passages(conn, CAT_Q, 5, "semantic", {}) == []


def test_archived_never_yields(store):
    item = _add(store, "Old cat", "cats of yore")
    with store.connect() as conn:
        conn.execute("UPDATE items SET archived_at='2026-01-01' WHERE id=%s", (item.id,))
    with store.reader() as conn:
        for mode in ("semantic", "hybrid"):
            assert sr.passages(conn, CAT_Q, 10, mode, PRIVATE, query="cats") == []
        assert sr.item_chunks(conn, item.id, PRIVATE) == []


def test_kind_and_item_ids_filter_before_cutoff(store):
    # many strong cat chunks in a document; one weak dog-ish article chunk
    _add(store, "Cats", "\n\n".join(["cat " * 200] * 4))
    art = _add(store, "Dogs", "dogs only", kind="article")
    with store.reader() as conn:
        top1 = sr.passages(conn, CAT_Q, 1, "semantic", PUBLIC)
        assert top1[0].kind == "document"
        only_art = sr.passages(conn, CAT_Q, 1, "semantic", PUBLIC, kind="article")
        assert len(only_art) == 1
        assert only_art[0].item_id == art.id
        only_id = sr.passages(conn, CAT_Q, 1, "hybrid", PUBLIC, item_ids=[art.id], query="cat")
        assert len(only_id) == 1
        assert only_id[0].item_id == art.id
        only_id = sr.passages(conn, CAT_Q, 5, "semantic", PUBLIC, item_ids=[art.id])
        assert {p.item_id for p in only_id} == {art.id}
        assert sr.passages(conn, CAT_Q, 5, "semantic", PUBLIC, item_ids=[]) == []
        assert sr.passages(conn, CAT_Q, 5, "semantic", PUBLIC, kind="book") == []


def test_item_chunks_lists_in_order_and_respects_visibility(store):
    pub = _add(store, "Felines", CAT_BODY)
    sec = _add(store, "Sec", "cats in secret", vis="private", owner="key-1")
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
