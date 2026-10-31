"""Search tests on Postgres full text and pgvector. The gateway is never contacted: embedders
are stubs, and the autouse no-network guard in conftest.py enforces it.

The index rows are written directly (``tests/core/search_helpers.py``); the indexer's own
tests (queueing, backoff, text extraction) wait for its port, plan task t13
(``tests/core/test_indexer_pending_t13.py``).
"""

import pytest

from culture_library.core import embedding_strategy as es
from culture_library.core import search as sr
from tests.core.search_helpers import PAIR, DownEmbed, add, count, fake_embed, index, vec

OTHER = es.Strategy("Retrieval-document", "Retrieval-query", "paragraph-pack", 1000, 3, "l2")


def test_title_searchable_immediately_without_indexing(store):
    add(store, "Dune Messiah")
    hits = sr.fulltext(store, "dune")
    assert [h.title for h in hits] == ["Dune Messiah"]
    assert hits[0].source == "fulltext"
    assert hits[0].corpus == "item"


def test_body_fulltext_with_snippet_after_indexing(store):
    item = add(store, "Notes", "the quick brown fox jumps over the lazy dog")
    assert sr.fulltext(store, "fox") == []
    index(store, item)
    hits = sr.fulltext(store, "fox")
    assert hits[0].item_id == item.id
    assert "[fox]" in hits[0].snippet


def test_snippet_is_about_twelve_words_around_the_match(store):
    words = [f"w{n}" for n in range(200)]
    words[120] = "aardvark"
    item = add(store, "Long", " ".join(words))
    index(store, item)
    snip = sr.fulltext(store, "aardvark")[0].snippet
    assert "[aardvark]" in snip
    assert 6 <= len(snip.split()) <= 13
    assert "w0 " not in snip


def test_title_match_outranks_body_only_match(store):
    body_only = add(store, "Ocean notes", "a long note that mentions the walrus once")
    titled = add(store, "Walrus", "about the sea")
    index(store, body_only)
    index(store, titled)
    hits = sr.fulltext(store, "walrus")
    assert [h.item_id for h in hits] == [titled.id, body_only.id]
    assert hits[0].score > hits[1].score


def test_stemming_cats_finds_cat(store):
    item = add(store, "Pets", "one cat sat on the mat")
    index(store, item)
    assert [h.item_id for h in sr.fulltext(store, "cats")] == [item.id]


def test_query_syntax_is_neutralised(store):
    add(store, "Alpha beta")
    assert sr.fulltext(store, "alpha\" ( & | ! ' :*") != []  # punctuation stripped
    assert sr.fulltext(store, "") == []
    assert sr.fulltext(store, '"*^ -') == []
    assert sr.tsquery_text("cat dog") == "'cat' & 'dog':*"
    assert sr.tsquery_text("it's x b:c") == "('it s' | 'it' & 's') & 'x' & ('b:c':* | 'b' & 'c':*)"
    assert sr.tsquery_text("\\' ! ") == ""


def test_dotted_and_hyphenated_words_match(store):
    item = add(store, "Notes", "x")
    index(store, item, body="see core/search.py and search.py, and mockup-harbor", embed=None)
    for q in ("search.py", "mockup-harbor", "harbor", "mockup harb"):
        assert [h.item_id for h in sr.fulltext(store, q)] == [item.id], q


def test_every_term_must_match(store):
    add(store, "Alpha beta")
    add(store, "Alpha gamma")
    assert [h.title for h in sr.fulltext(store, "alpha beta")] == ["Alpha beta"]


def test_prefix_match_on_last_term(store):
    add(store, "Constitution of India")
    assert len(sr.fulltext(store, "constit")) == 1
    assert sr.fulltext(store, "constit india") == []  # only the last term is a prefix
    assert len(sr.fulltext(store, "india constit")) == 1


def test_author_is_title_searchable(store):
    add(store, "Collected poems", author="Wislawa Szymborska")
    assert [h.title for h in sr.fulltext(store, "szymborska")] == ["Collected poems"]


def test_equal_scores_break_ties_on_id(store):
    ids = [add(store, f"Zebra {n}", f"zebra {n}").id for n in range(4)]
    hits = sr.fulltext(store, "zebra")
    assert [h.item_id for h in hits] == ids


def test_visibility_respected(store):
    add(store, "Secret plans", vis="private")
    add(store, "Public plans", vis="public")
    assert [h.title for h in sr.fulltext(store, "plans")] == ["Public plans"]
    both = sr.fulltext(store, "plans", include_private=True)
    assert {h.title for h in both} == {"Secret plans", "Public plans"}


def test_owner_sees_own_private_item(store):
    add(store, "Mine plans", vis="private", owner="key-1")
    add(store, "Theirs plans", vis="private", owner="key-2")
    assert [h.title for h in sr.fulltext(store, "plans", owner="key-1")] == ["Mine plans"]


def test_archived_hidden(store):
    item = add(store, "Old thing")
    with store.connect() as conn:
        conn.execute("UPDATE items SET archived_at='2026-01-01' WHERE id=%s", (item.id,))
    assert sr.fulltext(store, "old") == []


def test_limit(store):
    for n in range(5):
        add(store, f"Okapi {n}")
    assert len(sr.fulltext(store, "okapi", limit=3)) == 3
    assert sr.fulltext(store, "okapi", limit=0) == []


def test_semantic_ranking_and_visibility(store):
    cat = add(store, "Felines", "all about the cat family")
    dog = add(store, "Canines", "all about the dog family")
    priv = add(store, "Private cats", "cat cat cat", vis="private")
    for item in (cat, dog, priv):
        index(store, item)
    hits = sr.semantic(store, "cat", fake_embed)
    assert hits[0].item_id == cat.id
    assert hits[0].score == pytest.approx(1.0)
    assert hits[0].source == "semantic"
    assert priv.id not in [h.item_id for h in hits]
    assert dog.id in [h.item_id for h in hits]
    assert len({h.item_id for h in hits}) == len(hits)  # best chunk per item
    allhits = sr.semantic(store, "cat", fake_embed, include_private=True)
    assert priv.id in [h.item_id for h in allhits]


def test_semantic_finds_the_best_chunk_of_each_item_past_a_crowd(store):
    """One item with many near chunks must not crowd the others out of ``limit``."""
    crowd = add(store, "Crowd", "x")
    lone = add(store, "Lone", "y")
    n = 120
    with store.connect() as conn:
        sr.set_embeddings(conn, crowd.id, ["c"] * n, [vec(1.0, 0.01 * i) for i in range(n)], **PAIR)
        sr.set_embeddings(conn, lone.id, ["l"], [vec(1.0, 2.0)], **PAIR)
    hits = sr.semantic(store, "cat", fake_embed, limit=2)
    assert [h.item_id for h in hits] == [crowd.id, lone.id]


def test_semantic_never_scores_another_model_or_strategy(store):
    a = add(store, "Alpha", "cat a")
    b = add(store, "Beta", "cat b")
    with store.connect() as conn:
        sr.set_embeddings(conn, a.id, ["cat"], [vec(1.0)], **PAIR)
        sr.set_embeddings(
            conn, b.id, ["cat"], [vec(1.0)], model_id="other/model", strategy=es.CURRENT_STRATEGY
        )
        sr.set_embeddings(
            conn, b.id, ["cat"], [[1.0, 0.0, 0.0]], model_id=es.CURRENT_MODEL_ID, strategy=OTHER
        )
    assert [h.item_id for h in sr.semantic(store, "cat", fake_embed)] == [a.id]
    three = sr.semantic(
        store, "cat", lambda t: [[1.0, 0.0, 0.0]], model_id=es.CURRENT_MODEL_ID, strategy=OTHER
    )
    assert [h.item_id for h in three] == [b.id]
    assert count(store, "SELECT count(*) FROM embeddings WHERE item_id = %s", (b.id,)) == 2


def test_set_embeddings_replaces_only_its_own_pair(store):
    item = add(store, "Alpha", "cat")
    with store.connect() as conn:
        sr.set_embeddings(conn, item.id, ["a", "b"], [vec(1.0), vec(0.5)], **PAIR)
        sr.set_embeddings(
            conn, item.id, ["a"], [vec(1.0)], model_id="m2", strategy=es.CURRENT_STRATEGY
        )
        sr.set_embeddings(conn, item.id, ["z"], [vec(0.2)], **PAIR)
    with store.reader() as conn:
        rows = conn.execute(
            "SELECT model_id, chunk_no, text FROM embeddings ORDER BY model_id, chunk_no"
        ).fetchall()
    assert rows == [(es.CURRENT_MODEL_ID, 0, "z"), ("m2", 0, "a")]


def test_set_embeddings_rejects_bad_input(store):
    item = add(store, "Alpha", "cat")
    with store.connect() as conn:
        with pytest.raises(sr.EmbeddingError):
            sr.set_embeddings(conn, item.id, ["a"], [[1.0, 0.0]], **PAIR)
        with pytest.raises(ValueError):
            sr.set_embeddings(conn, item.id, ["a", "b"], [vec(1.0)], **PAIR)
        with pytest.raises(ValueError):
            sr.set_embeddings(conn, item.id, ["a"], [vec(float("nan"))], **PAIR)
        with pytest.raises(ValueError):
            sr.set_embeddings(
                conn, item.id, ["a"], [vec(1.0)], model_id="", strategy=es.CURRENT_STRATEGY
            )
        with pytest.raises(TypeError):  # the pair is required, by keyword
            sr.set_embeddings(conn, item.id, ["a"], [vec(1.0)], "gemma2-embed")


def test_semantic_wrong_dimension_is_an_embedding_error(store):
    add(store, "Alpha", "cat")
    with pytest.raises(sr.EmbeddingError):
        sr.semantic(store, "cat", lambda t: [[1.0, 0.0, 0.0]])


def test_zero_vector_is_refused(store):
    """A cosine vector needs a direction; pgvector's HNSW cosine index skips zero vectors, so
    one stored could never be found consistently."""
    item = add(store, "Zero", "x")
    with store.connect() as conn, pytest.raises(sr.EmbeddingError):
        sr.set_embeddings(conn, item.id, ["x"], [vec()], **PAIR)


def test_hybrid_fuses_with_rrf_k60(store):
    a = add(store, "Felines", "all about the cat family")
    b = add(store, "Cat dogs", "dog dog dog")
    for item in (a, b):
        index(store, item)
    ft = sr.fulltext(store, "cat", limit=40)
    sem = sr.semantic(store, "cat", fake_embed, limit=40)
    hyb = sr.hybrid(store, "cat", fake_embed)
    expect: dict = {}
    for ranked in (ft, sem):
        for pos, h in enumerate(ranked):
            expect[h.item_id] = expect.get(h.item_id, 0.0) + 1.0 / (60 + pos)
    assert {h.item_id: h.score for h in hyb} == pytest.approx(expect)
    assert all(h.source == "hybrid" for h in hyb)
    assert [h.score for h in hyb] == sorted((h.score for h in hyb), reverse=True)


def test_hybrid_falls_back_to_fulltext_when_embedder_down(store):
    item = add(store, "Felines", "all about the cat family")
    index(store, item)
    hits = sr.hybrid(store, "cat", DownEmbed())
    assert [h.title for h in hits] == ["Felines"]


def test_semantic_raises_when_embedder_down(store):
    with pytest.raises(sr.EmbeddingError):
        sr.semantic(store, "cat", DownEmbed())


def test_body_only_index_has_no_vectors(store):
    item = add(store, "Notes", "the narwhal surfaces at dawn")
    index(store, item, embed=None)
    assert sr.fulltext(store, "narwhal")[0].item_id == item.id
    assert sr.semantic(store, "narwhal", fake_embed) == []
    assert count(store, "SELECT COUNT(*) FROM embeddings") == 0


def test_set_body_keeps_title_and_index_title_keeps_body(store):
    item = add(store, "Heron", "x")
    index(store, item, body="the ibis wades", embed=None)
    with store.connect() as conn:
        sr.index_title(conn, item)
    assert sr.fulltext(store, "ibis")
    assert sr.fulltext(store, "heron")


def test_chunking():
    assert sr.chunk_text("") == []
    chunks = sr.chunk_text("para one\n\n" + ("word " * 600), max_chars=500)
    assert len(chunks) > 3
    assert all(len(c) <= 500 for c in chunks)


def test_vector_literal():
    assert sr.vector_literal([0.5, -1, 2e-7]) == "[0.5,-1.0,2e-07]"
    with pytest.raises(ValueError):
        sr.vector_literal([float("inf")])


def test_vector_cache_is_gone():
    import importlib.util

    assert importlib.util.find_spec("culture_library.core.vector_cache") is None
    for name in ("pack_vec", "unpack_vec", "cosine_scorer"):
        assert not hasattr(sr, name)


def test_no_vector_log_table_or_triggers(store):
    assert count(store, "SELECT to_regclass('vector_log') IS NULL")
    sql = (
        "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal"
        " AND tgrelid IN ('embeddings'::regclass, 'repodoc_embeddings'::regclass)"
    )
    assert count(store, sql) == 0
