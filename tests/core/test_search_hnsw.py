"""Semantic retrieval goes through the (model id, strategy)'s HNSW index, and its filters
still return complete, near-exact results (acceptance criterion 2 of plan task t12).

pgvector applies WHERE filters after the HNSW scan, so a filtered index query could come back
short or miss rows. search.py runs each index query with ``hnsw.iterative_scan =
relaxed_order`` and ``hnsw.ef_search >= search.EF_SEARCH``, re-sorts the candidates exactly,
and scores given ``item_ids`` exactly without the index; the planner itself picks an exact
plan (items first, then the embeddings primary key) for a narrow kind filter.

Measured while choosing this (a throwaway script on the test Postgres, pgvector 0.8.7, the
migration's index: m=16, ef_construction=64; 20 queries each), recall@k against exact
scoring, and how often a query came back short:

* 5,000 clustered vectors (50 centres, noise 0.6; embedding-like), ef_search 40, 100 or 200:
  recall 1.00 (0.998 at worst) for no filter, public only (50% of rows), kind (5%) and kind
  (0.5%), k=5 and k=50; never short. Index queries took 4-8 ms against 15-27 ms exact.
* 4,000 unstructured random vectors (the worst case for HNSW): with no filter, recall@5 was
  0.51 at ef_search 40, 0.80 at 100 and 0.93 at 200 (about 8, 10 and 11 ms); never short.
  Hence EF_SEARCH = 100. Narrow kind filters were exact at every setting (the planner did not
  use the index).

The tests below keep the clustered case as a regression check at a smaller size.
"""

from __future__ import annotations

import math
import random

import pytest

from culture_library.core import embedding_strategy as es
from culture_library.core import search as sr
from tests.core.search_helpers import PAIR

DIM = es.CURRENT_STRATEGY.dimension
INDEX = es.hnsw_index_name("embeddings", es.CURRENT_MODEL_ID, es.CURRENT_STRATEGY)
PAIR_T = (es.CURRENT_MODEL_ID, es.CURRENT_STRATEGY)
DOC_INDEX = es.hnsw_index_name("repodoc_embeddings", es.CURRENT_MODEL_ID, es.CURRENT_STRATEGY)


class Recorder:
    """A connection that records the statements search.py runs on it."""

    def __init__(self, conn):
        self.conn = conn
        self.statements: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.statements.append((sql, tuple(params or ())))
        return self.conn.execute(sql, params)

    def transaction(self):
        return self.conn.transaction()

    def vector_queries(self):
        return [(s, p) for s, p in self.statements if "<=>" in s]


def _unit(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def _cosine(a, b):
    return sum(x * y for x, y in zip(a, b))  # both unit length


@pytest.fixture
def corpus(store):
    """240 items x 8 chunks of clustered unit vectors: half public, 1 in 12 an article."""
    rng = random.Random(7)
    centres = [[rng.gauss(0, 1) for _ in range(DIM)] for _ in range(30)]

    def sample():
        c = rng.choice(centres)
        return _unit([x + rng.gauss(0, 0.6) for x in c])

    vectors: dict[tuple[int, int], list[float]] = {}
    meta: dict[int, tuple[str, str]] = {}
    with store.connect() as conn:
        for n in range(240):
            kind = "article" if n % 12 == 0 else "book"
            vis = "public" if n % 2 == 0 else "private"
            item_id = conn.execute(
                "INSERT INTO items (sha256, kind, title, format, size, visibility, created_at)"
                " VALUES (%s, %s, %s, 'md', 1, %s, '2026-10-31') RETURNING id",
                (f"sha{n}", kind, f"T{n}", vis),
            ).fetchone()[0]
            meta[item_id] = (kind, vis)
            vecs = [sample() for _ in range(8)]
            sr.set_embeddings(conn, item_id, [f"c{c}" for c in range(8)], vecs, **PAIR)
            for c, v in enumerate(vecs):
                vectors[(item_id, c)] = v
    with store.connect() as conn:
        conn.execute("ANALYZE embeddings")
        conn.execute("ANALYZE items")
    return store, vectors, meta, sample


def _truth(vectors, meta, q, k, keep):
    ranked = sorted(
        (key for key in vectors if keep(*meta[key[0]])),
        key=lambda key: (-_cosine(q, vectors[key]), key),
    )
    return ranked[:k]


def _plan(conn, sql, params, *, eligible=False) -> str:
    """EXPLAIN as search.py runs it. ``eligible`` disables sequential scans, to show the
    index can serve a query the planner costs as cheaper to scan on this small test table."""
    with conn.transaction():
        conn.execute("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)")
        if eligible:
            conn.execute("SET LOCAL enable_seqscan = off")
        rows = conn.execute("EXPLAIN " + sql, params).fetchall()
    return "\n".join(r[0] for r in rows)


def test_semantic_passages_use_the_pairs_hnsw_index(corpus):
    store, _, _, sample = corpus
    q = sample()
    with store.reader() as raw:
        conn = Recorder(raw)
        for vis in (sr.Vis(True), sr.Vis()):
            conn.statements.clear()
            assert len(sr.passages(conn, q, 10, "semantic", vis)) == 10
            (sql, params), *_ = conn.vector_queries()
            assert f"e.model_id = '{es.CURRENT_MODEL_ID}'" in sql  # literals: the partial index
            plan = _plan(raw, sql, params)
            assert f"Index Scan using {INDEX} on embeddings" in plan, plan
            assert "<=>" in plan


def test_semantic_hits_and_hybrid_use_the_index(corpus):
    store, _, _, sample = corpus
    q = sample()
    with store.reader() as raw:
        conn = Recorder(raw)
        sr.passages(conn, q, 5, "hybrid", sr.Vis(), query="")
        (sql, params), *_ = conn.vector_queries()
        assert f"Index Scan using {INDEX}" in _plan(raw, sql, params, eligible=True)
        conn.statements.clear()
        rows = sr._ann(conn, 0, sr.vector_literal(q), sr._visible(False, None), 80, PAIR_T)
        assert len(rows) == 80
        (sql, params), *_ = conn.vector_queries()
        assert f"Index Scan using {INDEX}" in _plan(raw, sql, params, eligible=True)


def test_repodoc_vectors_use_their_own_index(store):
    from culture_library.core.repodocs import RepoDocs

    docs = RepoDocs(store)
    rng = random.Random(3)
    for n in range(60):
        pushed = docs.push(
            remote="https://github.com/agentculture/x",
            path=f"docs/specs/s{n}.md",
            commit="a" * 40,
            committed_at="2026-10-01T00:00:00Z",
            default_branch_head="a" * 40,
            visibility="public",
            content=f"doc {n}",
        )
        vecs = [_unit([rng.gauss(0, 1) for _ in range(DIM)]) for _ in range(20)]
        with store.connect() as conn:
            sr.set_repodoc_text(conn, pushed.doc.id, f"S{n}", pushed.doc.path, "", "public")
            sr.set_repodoc_embeddings(conn, pushed.doc.id, ["x"] * 20, vecs, **PAIR)
    with store.connect() as conn:
        conn.execute("ANALYZE repodoc_embeddings, repodocs, repodoc_text")
    with store.reader() as raw:
        conn = Recorder(raw)
        got = sr.passages(conn, vecs[0], 5, "semantic", sr.Vis(), corpora=("repodoc",))
        assert got[0].text == "x"
        assert got[0].score == pytest.approx(1.0)
        (sql, params), *_ = conn.vector_queries()
        plan = _plan(raw, sql, params, eligible=True)
        assert f"Index Scan using {DOC_INDEX} on repodoc_embeddings" in plan, plan


FILTERS = [
    ("all", sr.Vis(True), None, lambda kind, vis: True),
    ("public", sr.Vis(), None, lambda kind, vis: vis == "public"),
    ("article", sr.Vis(True), "article", lambda kind, vis: kind == "article"),
    ("public article", sr.Vis(), "article", lambda k, v: k == "article" and v == "public"),
]


def test_filtered_index_queries_are_complete_and_near_exact(corpus):
    store, vectors, meta, sample = corpus
    queries = [sample() for _ in range(8)]
    for label, vis, kind, keep in FILTERS:
        recalls = []
        for q in queries:
            for k in (5, 40):
                with store.reader() as conn:
                    got = sr.passages(conn, q, k, "semantic", vis, kind)
                truth = _truth(vectors, meta, q, k, keep)
                assert len(got) == len(truth), label  # never short
                found = {(p.item_id, p.chunk_no) for p in got}
                recalls.append(len(found & set(truth)) / len(truth))
                assert [p.score for p in got] == sorted((p.score for p in got), reverse=True)
        assert sum(recalls) / len(recalls) >= 0.95, (label, recalls)


def test_item_ids_are_scored_exactly_without_the_index(corpus):
    store, vectors, meta, sample = corpus
    ids = sorted(meta)[:3]
    q = sample()
    with store.reader() as raw:
        conn = Recorder(raw)
        got = sr.passages(conn, q, 24, "semantic", sr.Vis(True), item_ids=ids)
        for sql, params in conn.vector_queries():
            assert INDEX not in _plan(raw, sql, params)
    truth = sorted(
        (key for key in vectors if key[0] in ids), key=lambda key: (-_cosine(q, vectors[key]), key)
    )
    assert [(p.item_id, p.chunk_no) for p in got] == truth
    for p in got:
        assert p.score == pytest.approx(_cosine(q, vectors[(p.item_id, p.chunk_no)]), abs=1e-5)


def test_semantic_hits_match_exact_best_chunk_per_item(corpus):
    store, vectors, meta, sample = corpus
    q = sample()
    hits = sr.semantic(store, "q", lambda texts: [q], include_private=True, limit=10)
    best: dict[int, float] = {}
    for (item_id, _), v in vectors.items():
        best[item_id] = max(best.get(item_id, -2.0), _cosine(q, v))
    truth = sorted(best, key=lambda i: (-best[i], i))[:10]
    got = [h.item_id for h in hits]
    assert len(got) == 10
    assert len(set(got) & set(truth)) >= 9


def test_exact_ties_at_the_pool_edge_rank_the_same_for_every_k(store):
    """Identical vectors tie exactly; HNSW cannot order them, so the pool's LIMIT may cut a tie
    group anywhere. Ranking must still be deterministic: ties break on (corpus, id, chunk_no)
    and every k is a prefix of a larger k, as on SQLite."""
    from tests.core.search_helpers import add, index

    same = lambda texts: [es_vec(1.0, 0.0) for _ in texts]  # noqa: E731 - every chunk ties
    items = [add(store, f"Twin {n}", f"twin body {n}\n\nmore twin text {n}") for n in range(12)]
    for item in items:
        index(store, item, embed=same)
    q = es_vec(1.0, 0.0)
    runs = {}
    for k in (1, 2, 3, 5, 8, 13):
        for _ in range(5):
            with store.reader() as conn:
                got = [
                    (p.item_id, p.chunk_no) for p in sr.passages(conn, q, k, "semantic", sr.Vis())
                ]
            runs.setdefault(k, set()).add(tuple(got))
    assert all(len(v) == 1 for v in runs.values()), runs
    widest = next(iter(runs[13]))
    assert widest == tuple(sorted(widest))
    for k, v in runs.items():
        assert next(iter(v)) == widest[:k]


def es_vec(*head):
    from tests.core.search_helpers import vec

    return vec(*head)
