"""The in-process vector cache (#25): same results as reading SQLite, always current."""

from __future__ import annotations

import sqlite3

import pytest

from culture_library.core import Store
from culture_library.core import indexer as ix
from culture_library.core import search as sr
from culture_library.core import vector_cache as vc
from culture_library.core.repodocs import RepoDocs

PUBLIC = sr.Vis()
CAT, DOG = [1.0, 0.0, 0.1], [0.0, 1.0, 0.1]
REMOTE = "https://github.com/agentculture/lobes-cli"


def fake_embed(texts):
    return [[float("cat" in t.lower()), float("dog" in t.lower()), 0.1] for t in texts]


@pytest.fixture(autouse=True)
def _fresh_cache():
    vc.clear()
    yield
    vc.clear()


def _add(store, title, body, kind="document"):
    res = store.add_item(body.encode(), kind=kind, title=title, format="md", visibility="public")
    with store.connect() as conn:
        ix.index_on_add(conn, res.item)
    return res.item


@pytest.fixture
def lib(store):
    cat = _add(store, "Felines", "cats purr\n\nweather filler")
    dog = _add(store, "Canines", "dogs bark\n\nmore dog things", kind="article")
    ix.Indexer(store, embed=fake_embed).run_once()
    return store, cat, dog


def _passages(store, q, mode="semantic", **kw):
    with store.reader() as conn:
        return sr.passages(conn, q, 10, mode, PUBLIC, query="cats", **kw)


def _key(p):
    return (p.item_id, p.doc_id, p.chunk_no, p.chunk_sha, p.text, round(p.score, 12))


def test_cached_results_equal_a_cold_reload(lib):
    store, _, _ = lib
    for mode in ("semantic", "hybrid"):
        warm1 = _passages(store, CAT, mode)
        warm2 = _passages(store, CAT, mode)
        vc.clear()
        cold = _passages(store, CAT, mode)
        assert [_key(p) for p in warm1] == [_key(p) for p in cold]
        assert [_key(p) for p in warm2] == [_key(p) for p in cold]
    hits = sr.semantic(store, "cats", fake_embed)
    vc.clear()
    assert [(h.item_id, h.snippet, h.score) for h in hits] == [
        (h.item_id, h.snippet, h.score) for h in sr.semantic(store, "cats", fake_embed)
    ]


def test_full_text_is_returned_for_the_top_passages(lib):
    store, cat, _ = lib
    top = _passages(store, CAT)[0]
    assert top.item_id == cat.id
    assert top.text.startswith("cats purr")


def test_re_embedding_an_item_is_seen_by_the_next_query(lib):
    store, cat, dog = lib
    assert _passages(store, CAT)[0].item_id == cat.id
    with store.connect() as conn:  # the cat item now embeds like a dog, and vice versa
        sr.set_embeddings(conn, cat.id, ["now about dogs"], [DOG])
        sr.set_embeddings(conn, dog.id, ["now about cats"], [CAT])
    top = _passages(store, CAT)[0]
    assert top.item_id == dog.id
    assert top.text == "now about cats"


def test_deleted_item_vectors_leave_the_cache(lib):
    store, cat, _ = lib
    _passages(store, CAT)
    with store.connect() as conn:
        conn.execute("DELETE FROM embeddings WHERE item_id = ?", (cat.id,))
    assert all(p.item_id != cat.id for p in _passages(store, CAT))


def test_a_pruned_log_falls_back_to_a_full_reload(lib):
    store, cat, dog = lib
    _passages(store, CAT)
    with store.connect() as conn:
        sr.set_embeddings(conn, cat.id, ["now about dogs"], [DOG])
        sr.set_embeddings(conn, dog.id, ["now about cats"], [CAT])
        conn.execute("DELETE FROM vector_log")  # older than the cache's last seq
        sr.set_embeddings(conn, dog.id, ["now about cats"], [CAT])
    assert _passages(store, CAT)[0].item_id == dog.id


def test_prune_log_drops_only_old_rows(lib):
    store, _, _ = lib
    with store.connect() as conn:
        before = conn.execute("SELECT count(*) FROM vector_log").fetchone()[0]
        assert before > 0
        assert vc.prune_log(conn) == 0
        assert vc.prune_log(conn, now=10**12) == before


def test_a_warm_query_reads_no_vector_blobs(lib, monkeypatch):
    store, _, _ = lib
    _passages(store, CAT)
    sql = []
    real = store._open

    def traced():
        conn = real()
        conn.set_trace_callback(sql.append)
        return conn

    monkeypatch.setattr(store, "_open", traced)
    _passages(store, CAT, "hybrid")
    assert sql
    assert not any("vec" in s and "vector_log" not in s for s in sql)


def test_repodoc_vectors_are_cached_and_current(store):
    docs = RepoDocs(store)
    docs.push(
        remote=REMOTE,
        path="docs/specs/s.md",
        commit="a" * 40,
        committed_at="2026-10-01T00:00:00Z",
        default_branch_head="a" * 40,
        visibility="public",
        content="cats everywhere\n",
    )
    ix.Indexer(store, embed=fake_embed).run_once()
    got = _passages(store, CAT, corpora=("repodoc",))
    assert got
    assert all(p.corpus == "repodoc" for p in got)
    assert any("cats everywhere" in p.text for p in got)
    with store.connect() as conn:
        doc_id = conn.execute("SELECT id FROM repodocs").fetchone()[0]
        conn.execute("DELETE FROM repodoc_embeddings WHERE doc_id = ?", (doc_id,))
    assert _passages(store, CAT, corpora=("repodoc",)) == []


def test_vector_log_and_triggers_are_created_on_an_old_database(tmp_path):
    data = tmp_path / "data"
    Store(data)
    conn = sqlite3.connect(data / "library.db")
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%_vlog'"):
        conn.execute(f"DROP TRIGGER {name}")
    conn.execute("DROP TABLE vector_log")
    conn.commit()
    conn.close()
    store = Store(data)
    with store.reader() as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "vector_log" in names
    assert {f"embeddings_{e}_vlog" for e in ("insert", "update", "delete")} <= names


def test_the_indexer_prunes_old_log_rows(lib):
    store, _, _ = lib
    with store.connect() as conn:
        conn.execute("UPDATE vector_log SET at = 0")
    ix.Indexer(store, embed=fake_embed).run_once()
    with store.reader() as conn:
        assert conn.execute("SELECT count(*) FROM vector_log WHERE at = 0").fetchone()[0] == 0
