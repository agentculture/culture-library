"""The background indexer on Postgres (plan task t13): the job queues' state machine, priority,
retries and backoff, text extraction, repo doc discovery and indexing, and the (model id,
strategy) every vector carries with the HNSW index its first vector creates.

The queue and repo doc tests were ported from the SQLite suite (tests/core/test_search.py and
tests/core/test_repodoc_index.py, parked by t12 in test_indexer_pending_t13.py). Embeddings
are the 768-d fakes of tests/core/search_helpers.py; no gateway is ever contacted.
"""

from __future__ import annotations

import json

import pytest

from culture_library.core import embedding_strategy as es
from culture_library.core import indexer as ix
from culture_library.core import search as sr
from culture_library.core.repodocs import RepoDocs
from tests.core.search_helpers import DownEmbed, fake_embed

A = "a" * 40
B = "b" * 40
REMOTE = "https://github.com/agentculture/lobes-cli"
BOTH = ("item", "repodoc")


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


_SHIPPED_HNSW = {
    es.hnsw_index_name(t, es.CURRENT_MODEL_ID, es.CURRENT_STRATEGY)
    for t in ("embeddings", "repodoc_embeddings")
}


@pytest.fixture(autouse=True)
def _drop_created_hnsw_indexes(store):
    """Indexes outlive the per-test truncate: drop the ones a test's new pair created, so the
    worker database keeps only the migration's (tests/server/test_status_database.py counts)."""
    yield
    with store.connect() as conn:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
                " AND indexname LIKE %s",
                ("%\\_hnsw\\_%",),
            )
        ]
        for name in sorted(set(names) - _SHIPPED_HNSW):
            conn.execute(f'DROP INDEX IF EXISTS "{name}"')


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
    with store.reader() as conn:
        return conn.execute(sql, params).fetchone()[0]


def _row(store, sql, params=()):
    with store.reader() as conn:
        return tuple(conn.execute(sql, params).fetchone())


def _doc_hits(store, q, **kw):
    return [h for h in sr.fulltext(store, q, corpora=BOTH, **kw) if h.corpus == "repodoc"]


# ---------------------------------------------------------------- queue (ported)


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
        body = conn.execute("SELECT body FROM item_text WHERE item_id = %s", (md.id,)).fetchone()[0]
    assert "**" not in body
    assert "#" not in body


def test_body_only_jobs_get_embeddings_once_an_embedder_appears(store):
    item = _add(store, "Felines", b"all about the cat family")
    ix.Indexer(store).run_once()
    assert ix.requeue_unembedded(store) == 1
    ix.Indexer(store, embed=fake_embed).run_once()
    assert sr.semantic(store, "cat", fake_embed)[0].item_id == item.id
    assert ix.requeue_unembedded(store) == 0


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
    body = _count(store, "SELECT body FROM repodoc_text")
    assert "Gondolas carry the mail" in body
    assert "{" not in body
    assert '"claims"' not in body


def test_binary_design_indexes_only_path_and_title(store, docs):
    _commit(docs, "docs/design/mockup-harbor.png", b"\x89PNG\r\n\x1a\nquokka\x00\xff")
    ix.Indexer(store).run_once()
    hits = _doc_hits(store, "harbor")
    assert len(hits) == 1
    assert hits[0].kind == "design"
    assert _doc_hits(store, "quokka") == []
    assert _count(store, "SELECT body FROM repodoc_text") == ""


def test_new_push_is_rediscovered_and_reindexed(store, docs):
    idx = ix.Indexer(store)
    _commit(docs, "docs/specs/s.md", "first ibis\n")
    idx.run_once()
    _commit(docs, "docs/specs/s.md", "second heron\n", commit=B, when="2026-10-02T00:00:00Z")
    idx.run_once()
    assert _doc_hits(store, "heron")
    assert _doc_hits(store, "ibis") == []


def test_removed_doc_is_dropped_from_the_index(store, docs):
    _commit(docs, "docs/specs/s.md", "cormorant cats\n")
    idx = ix.Indexer(store, embed=fake_embed)
    idx.run_once()
    assert _count(store, "SELECT count(*) FROM repodoc_embeddings") > 0
    docs.tombstone(REMOTE, "docs/specs/s.md", B)
    idx.run_once()
    assert _doc_hits(store, "cormorant") == []
    assert _count(store, "SELECT count(*) FROM repodoc_text") == 0
    assert _count(store, "SELECT count(*) FROM repodoc_embeddings") == 0
    assert _count(store, "SELECT count(*) FROM repodoc_index_queue") == 0


def test_removed_doc_is_never_enqueued(store, docs):
    _commit(docs, "docs/specs/s.md", "cormorant\n")
    docs.tombstone(REMOTE, "docs/specs/s.md", B)
    assert ix.discover_repodocs(store) == 0
    assert _count(store, "SELECT count(*) FROM repodoc_index_queue") == 0


def test_item_enqueued_later_is_indexed_before_queued_repodocs(store, docs):
    for n in range(3):
        _commit(docs, f"docs/specs/s{n}.md", f"backfill doc {n}\n")
    assert ix.discover_repodocs(store) == 3
    item = _add_item(store, "Fresh upload", b"fresh")
    idx = ix.Indexer(store)
    assert idx.run_once(limit=1) == 1
    assert _count(store, "SELECT state FROM index_queue WHERE item_id = %s", (item.id,)) == "done"
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
    assert (
        _count(store, "SELECT state FROM index_queue WHERE item_id = %s", (added[0].id,)) == "done"
    )


def test_gateway_failure_leaves_repodoc_jobs_queued_and_they_resume(store, docs):
    clock = [1000.0]
    emb = DownEmbed()
    idx = ix.Indexer(store, embed=emb, now=lambda: clock[0], backoff_base=10, backoff_cap=100)
    _commit(docs, "docs/specs/s.md", "cats in the spec\n")
    assert idx.run_once() == 0
    with store.reader() as conn:
        row = conn.execute(
            "SELECT state, attempts, next_attempt_at, last_error FROM repodoc_index_queue"
        ).fetchone()
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
    assert title == "f.json"
    assert "not json" in body


# ---------------------------------------------------------------- queue (new on Postgres)


def test_index_on_add_with_body_marks_the_job_body_done(store):
    item = store.add_item(b"x", kind="document", title="T", format="md").item
    with store.connect() as conn:
        ix.index_on_add(conn, item, "the pelican body")
    assert _count(store, "SELECT body_done FROM index_queue WHERE item_id = %s", (item.id,))
    assert sr.fulltext(store, "pelican", include_private=True)[0].item_id == item.id


def test_items_run_by_priority_then_age(store):
    clock = [100.0]
    first = store.add_item(b"1", kind="document", title="First", format="md").item
    second = store.add_item(b"2", kind="document", title="Second", format="md").item
    with store.connect() as conn:
        ix.enqueue(conn, first.id, now=1.0)
        ix.enqueue(conn, second.id, now=2.0)
        conn.execute("UPDATE index_queue SET priority = -1 WHERE item_id = %s", (second.id,))
    idx = ix.Indexer(store, now=lambda: clock[0])
    assert idx._due_items(2) == [second.id, first.id]


def test_backoff_doubles_and_caps(store):
    clock = [0.0]
    idx = ix.Indexer(store, embed=DownEmbed(), now=lambda: clock[0], backoff_base=2, backoff_cap=5)
    item = _add(store, "Felines", b"cat")
    delays = []
    for _ in range(4):
        idx.run_once()
        nxt = _count(
            store, "SELECT next_attempt_at FROM index_queue WHERE item_id = %s", (item.id,)
        )
        delays.append(nxt - clock[0])
        clock[0] = nxt
    assert delays == [2.0, 4.0, 5.0, 5.0]
    assert _count(store, "SELECT attempts FROM index_queue") == 4


def test_status_meta_is_upserted(store):
    idx = ix.Indexer(store, embed=DownEmbed(), now=lambda: 10.0)
    _add(store, "A", b"a")
    idx.run_once()
    with store.connect() as conn:
        conn.execute("UPDATE index_queue SET next_attempt_at = 0")
    idx.run_once()  # a second failure rewrites the meta rows instead of adding more
    assert _count(store, "SELECT count(*) FROM index_meta WHERE key = 'last_error'") == 1


# ---------------------------------------------------------------- model id + strategy (c42)

OTHER_MODEL = "Qwen/Qwen3-Embedding-0.6B"
SMALL = es.Strategy(
    document_prompt="",
    query_prompt="",
    chunker="paragraph-pack",
    max_chars=sr.CHUNK_MAX_CHARS,
    dimension=4,
    normalisation="l2",
)


def small_embed(texts):
    return [[1.0, float("cat" in t.lower()), 0.5, 0.25] for t in texts]


class SpaceEmbed:
    """An embedder that states its vector space, as GatewayEmbedder does from /capabilities."""

    def __init__(self, space, fn=small_embed):
        self.space = space
        self.fn = fn
        self.lookups = 0

    def vector_space(self):
        self.lookups += 1
        if isinstance(self.space, Exception):
            raise self.space
        return self.space

    def __call__(self, texts):
        return self.fn(texts)


def _pairs(store, table="embeddings"):
    with store.reader() as conn:
        return {
            (r[0], r[1])
            for r in conn.execute(f"SELECT DISTINCT model_id, strategy FROM {table}")  # nosec
        }


def _index_exists(store, name):
    return _count(store, "SELECT count(*) FROM pg_indexes WHERE indexname = %s", (name,)) == 1


def _drop_index(store, table, model_id, strategy):
    with store.connect() as conn:
        conn.execute(f"DROP INDEX IF EXISTS {es.hnsw_index_name(table, model_id, strategy)}")


def test_strategy_for_today_matches_the_current_strategy():
    got = ix.strategy_for(
        document_prompt="Retrieval-document",
        query_prompt="Retrieval-query",
        dimension=768,
        normalisation="l2",
    )
    assert got == es.CURRENT_STRATEGY
    assert got.key() == es.CURRENT_STRATEGY.key()


def test_a_plain_callable_writes_the_current_pair(store):
    _add(store, "Felines", b"cat body")
    ix.Indexer(store, embed=fake_embed).run_once()
    assert _pairs(store) == {(es.CURRENT_MODEL_ID, es.CURRENT_STRATEGY.key())}


def test_every_vector_carries_the_embedders_model_id_and_strategy(store, docs):
    _drop_index(store, "embeddings", OTHER_MODEL, SMALL)
    _drop_index(store, "repodoc_embeddings", OTHER_MODEL, SMALL)
    item = _add(store, "Felines", b"cat body\n\nmore text")
    _commit(docs, "docs/specs/s.md", "cats in the spec\n")
    emb = SpaceEmbed((OTHER_MODEL, SMALL))
    assert ix.Indexer(store, embed=emb).run_once() == 2
    assert _pairs(store) == {(OTHER_MODEL, SMALL.key())}
    assert _pairs(store, "repodoc_embeddings") == {(OTHER_MODEL, SMALL.key())}
    assert _count(store, "SELECT count(*) FROM embeddings WHERE vector_dims(vec) <> 4") == 0
    # the stored pair is what a query must name to find them
    hits = sr.semantic(store, "cat", small_embed, model_id=OTHER_MODEL, strategy=SMALL)
    assert hits[0].item_id == item.id
    assert sr.semantic(store, "cat", fake_embed) == []  # the current pair has no vectors


def test_explicit_pair_wins_and_must_be_whole(store):
    _drop_index(store, "embeddings", OTHER_MODEL, SMALL)
    _add(store, "Felines", b"cat body")
    ix.Indexer(store, embed=small_embed, model_id=OTHER_MODEL, strategy=SMALL).run_once()
    assert _pairs(store) == {(OTHER_MODEL, SMALL.key())}
    with pytest.raises(ValueError):
        ix.Indexer(store, embed=small_embed, model_id=OTHER_MODEL)
    with pytest.raises(ValueError):
        ix.Indexer(store, embed=small_embed, model_id=" ", strategy=SMALL)


def test_first_vector_of_a_new_pair_creates_its_hnsw_index(store, docs):
    item_index = es.hnsw_index_name("embeddings", OTHER_MODEL, SMALL)
    doc_index = es.hnsw_index_name("repodoc_embeddings", OTHER_MODEL, SMALL)
    _drop_index(store, "embeddings", OTHER_MODEL, SMALL)
    _drop_index(store, "repodoc_embeddings", OTHER_MODEL, SMALL)
    assert not _index_exists(store, item_index)
    _add(store, "Felines", b"cat body")
    _commit(docs, "docs/specs/s.md", "cats\n")
    ix.Indexer(store, embed=SpaceEmbed((OTHER_MODEL, SMALL))).run_once()
    assert _index_exists(store, item_index)
    assert _index_exists(store, doc_index)
    indexdef = _count(store, "SELECT indexdef FROM pg_indexes WHERE indexname = %s", (item_index,))
    assert "USING hnsw (((vec)::vector(4)) vector_cosine_ops)" in indexdef
    assert OTHER_MODEL in indexdef and SMALL.key() in indexdef
    # the planner can use it for a query of that pair
    q = sr.vector_literal([1.0, 1.0, 0.5, 0.25])
    with store.reader() as conn:
        conn.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(
            r[0]
            for r in conn.execute(
                "EXPLAIN SELECT item_id FROM embeddings e WHERE "
                f"e.model_id = '{OTHER_MODEL}' AND e.strategy = '{SMALL.key()}'"
                f" ORDER BY e.vec::vector(4) <=> '{q}'::vector(4) LIMIT 5"
            )
        )
    assert item_index in plan, plan


def test_ensure_hnsw_index_is_idempotent_and_skips_wide_strategies(store):
    _drop_index(store, "embeddings", OTHER_MODEL, SMALL)
    with store.connect() as conn:
        assert ix.ensure_hnsw_index(conn, "embeddings", OTHER_MODEL, SMALL) is True
        assert ix.ensure_hnsw_index(conn, "embeddings", OTHER_MODEL, SMALL) is False
    wide = es.Strategy("", "", "paragraph-pack", 1000, ix.HNSW_MAX_DIMENSION + 1, "l2")
    with store.connect() as conn:
        assert ix.ensure_hnsw_index(conn, "embeddings", OTHER_MODEL, wide) is False
    assert not _index_exists(store, es.hnsw_index_name("embeddings", OTHER_MODEL, wide))


def test_index_creation_rolls_back_with_a_failed_write(store):
    _drop_index(store, "embeddings", OTHER_MODEL, SMALL)
    _add(store, "Felines", b"cat body")
    bad = SpaceEmbed((OTHER_MODEL, SMALL), fn=lambda texts: [[0.0] * 4 for _ in texts])
    ix.Indexer(store, embed=bad).run_once()  # zero vectors: set_embeddings refuses them
    assert "zero vector" in ix.status(store)["last_error"]
    assert not _index_exists(store, es.hnsw_index_name("embeddings", OTHER_MODEL, SMALL))


def test_unresolvable_vector_space_fails_the_job_with_backoff_and_writes_nothing(store, docs):
    clock = [1000.0]
    emb = SpaceEmbed(sr.EmbeddingError("cannot read /capabilities: refused"))
    idx = ix.Indexer(store, embed=emb, now=lambda: clock[0], backoff_base=10)
    item = _add(store, "Felines", b"cat body")
    _commit(docs, "docs/specs/s.md", "cats\n")
    assert idx.run_once() == 0
    for table, key in (("index_queue", "item_id"), ("repodoc_index_queue", "doc_id")):
        row = _row(store, f"SELECT state, attempts, next_attempt_at FROM {table}")  # nosec
        assert row == ("pending", 1, 1010.0), table
    assert "capabilities" in ix.status(store)["last_error"]
    assert _count(store, "SELECT count(*) FROM embeddings") == 0
    assert _count(store, "SELECT count(*) FROM repodoc_embeddings") == 0
    assert sr.fulltext(store, "cat")[0].item_id == item.id  # full text does not wait for it
    emb.space = (OTHER_MODEL, SMALL)  # the gateway answers again
    clock[0] += 11
    assert idx.run_once() == 2
    assert _pairs(store) == {(OTHER_MODEL, SMALL.key())}


def test_requeue_stale_model_reopens_jobs_lacking_the_pair(store):
    item = _add(store, "Felines", b"cat body")
    ix.Indexer(store, embed=fake_embed).run_once()
    assert ix.requeue_stale_model(store, es.CURRENT_MODEL_ID, es.CURRENT_STRATEGY) == 0
    assert ix.requeue_stale_model(store, OTHER_MODEL, SMALL) == 1
    row = _row(store, "SELECT state, body_done FROM index_queue WHERE item_id = %s", (item.id,))
    assert row == ("pending", True)
    assert ix.requeue_stale_model(store, "", SMALL) == 0


def test_a_new_pair_re_embeds_the_corpus_on_the_first_run(store, docs):
    _drop_index(store, "embeddings", OTHER_MODEL, SMALL)
    _add(store, "Felines", b"cat body")
    _commit(docs, "docs/specs/s.md", "cats\n")
    ix.Indexer(store, embed=fake_embed).run_once()
    assert ix.status(store)["done"] == 1
    emb = SpaceEmbed((OTHER_MODEL, SMALL))
    assert ix.Indexer(store, embed=emb).run_once() == 2  # both re-opened and re-embedded
    assert _pairs(store) == {
        (es.CURRENT_MODEL_ID, es.CURRENT_STRATEGY.key()),
        (OTHER_MODEL, SMALL.key()),
    }  # the old pair's vectors stay beside the new ones
    assert _pairs(store, "repodoc_embeddings") == _pairs(store)


def test_the_item_body_is_read_from_item_text(store):
    item = _add(store, "Notes", b"ignored bytes")
    with store.connect() as conn:
        sr.set_body(conn, item, "stored body about cats")
        conn.execute("UPDATE index_queue SET body_done = true")
    ix.Indexer(store, embed=fake_embed).run_once()
    with store.reader() as conn:
        texts = [r[0] for r in conn.execute("SELECT text FROM embeddings ORDER BY chunk_no")]
    assert texts == ["Notes", "stored body about cats"]


# ---------------------------------------------------------------- punctuated words (risk r5)


def test_a_path_in_a_body_is_found_by_its_words(store, docs):
    item = _add(store, "Notes", b"see core/search.py for the ranking")
    _commit(docs, "docs/specs/s.md", "the port touched culture_library/core/indexer.py\n")
    ix.Indexer(store).run_once()
    assert [h.item_id for h in sr.fulltext(store, "search")] == [item.id]
    assert [h.item_id for h in sr.fulltext(store, "core/search.py")] == [item.id]
    assert [h.item_id for h in sr.fulltext(store, "ranking")] == [item.id]
    assert len(_doc_hits(store, "indexer")) == 1
    assert len(_doc_hits(store, "culture_library")) == 1


def test_split_words_leave_unpunctuated_text_unchanged(store):
    item = _add(store, "Cats Running", b"x")
    with store.connect() as conn:
        sr.set_body(conn, item, "the dogs jumped")
    tsv = _count(store, "SELECT tsv::text FROM item_text WHERE item_id = %s", (item.id,))
    assert tsv == "'cat':1A 'dog':4B 'jump':5B 'run':2A"


def test_split_words_keep_non_ascii_letters_whole(store):
    item = _add(store, "Menu", b"x")
    with store.connect() as conn:
        sr.set_body(conn, item, "café-crème and naïve")
    tsv = _count(store, "SELECT tsv::text FROM item_text WHERE item_id = %s", (item.id,))
    assert "'caf'" not in tsv and "'café'" in tsv and "'crème'" in tsv
    assert sr.fulltext(store, "crème")[0].item_id == item.id
