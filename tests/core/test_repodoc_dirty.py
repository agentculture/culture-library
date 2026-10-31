"""Repo doc discovery reads only changed docs (#39): an idle tick never scans ``repodocs``."""

from __future__ import annotations

import sqlite3

import pytest

from culture_library.core import Store
from culture_library.core import indexer as ix
from culture_library.core.repodocs import RepoDocs

A = "a" * 40
B = "b" * 40
REMOTE = "https://github.com/agentculture/lobes-cli"


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


def _rows(store, sql, params=()):
    conn = sqlite3.connect(store.db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _queued(store):
    return {
        r[0]: r[1] for r in _rows(store, "SELECT doc_id, shown_sha256 FROM repodoc_index_queue")
    }


class _Trace:
    """Collect every SQL statement the store's connections run."""

    def __init__(self, store, monkeypatch):
        self.sql: list[str] = []
        real = store._open

        def traced():
            conn = real()
            conn.set_trace_callback(self.sql.append)
            return conn

        monkeypatch.setattr(store, "_open", traced)

    def scanned_repodocs(self) -> bool:
        return any("FROM repodocs d" in s for s in self.sql)


def test_idle_discovery_does_not_scan_repodocs(store, docs, monkeypatch):
    for n in range(50):
        _commit(docs, f"docs/specs/s{n}.md", f"spec {n}\n")
    assert ix.discover_repodocs(store) == 50
    trace = _Trace(store, monkeypatch)
    assert ix.discover_repodocs(store) == 0
    assert not trace.scanned_repodocs()


def test_idle_tick_does_not_scan_repodocs(store, docs, monkeypatch):
    _commit(docs, "docs/specs/s.md", "spec\n")
    idx = ix.Indexer(store)
    idx.run_once()
    trace = _Trace(store, monkeypatch)
    idx.run_once()
    assert not trace.scanned_repodocs()


def test_one_push_plans_only_that_doc(store, docs, monkeypatch):
    for n in range(20):
        _commit(docs, f"docs/specs/s{n}.md", f"spec {n}\n")
    ix.discover_repodocs(store)
    _commit(docs, "docs/specs/s3.md", "changed\n", commit=B, when="2026-10-02T00:00:00Z")
    trace = _Trace(store, monkeypatch)
    assert ix.discover_repodocs(store) == 1
    planned = [s for s in trace.sql if "FROM repodocs d" in s]
    assert planned
    assert all("repodoc_dirty" in s for s in planned)


def test_draft_versions_tombstones_and_visibility_all_mark_dirty(store, docs):
    _commit(docs, "docs/specs/s.md", "first\n")
    ix.discover_repodocs(store)
    first = _queued(store)
    docs.push(remote=REMOTE, path="docs/specs/s.md", commit="working", content="draft\n")
    assert ix.discover_repodocs(store) == 1
    assert _queued(store) != first
    _commit(docs, "docs/specs/s.md", "draft\n", commit=B, when="2026-10-02T00:00:00Z")
    ix.discover_repodocs(store)
    docs.tombstone(REMOTE, "docs/specs/s.md", "c" * 40)
    ix.discover_repodocs(store)
    assert _queued(store) == {}


def test_dirty_marks_are_cleared_after_discovery(store, docs):
    _commit(docs, "docs/specs/s.md", "x\n")
    assert _rows(store, "SELECT count(*) FROM repodoc_dirty")[0][0] == 1
    ix.discover_repodocs(store)
    assert _rows(store, "SELECT count(*) FROM repodoc_dirty")[0][0] == 0


def test_upgrade_seeds_every_existing_doc_once(tmp_path):
    data = tmp_path / "data"
    store = Store(data)
    docs = RepoDocs(store)
    for n in range(3):
        _commit(docs, f"docs/specs/s{n}.md", f"spec {n}\n")
    conn = sqlite3.connect(data / "library.db")
    for name in [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")]:
        conn.execute(f"DROP TRIGGER {name}")
    conn.execute("DROP TABLE repodoc_dirty")
    conn.execute("DELETE FROM repodoc_index_queue")
    conn.commit()
    conn.close()
    store = Store(data)  # the release before #39 had no dirty table: the first open seeds it
    assert ix.discover_repodocs(store) == 3
    Store(data)  # a later open never re-seeds
    assert ix.discover_repodocs(store) == 0


def _dirty(store):
    return {r[0] for r in _rows(store, "SELECT doc_id FROM repodoc_dirty")}


def test_identical_repush_marks_nothing(store, docs):
    _commit(docs, "docs/specs/s.md", "same\n")
    docs.push(remote=REMOTE, path="docs/specs/s.md", commit="working", content="draft\n")
    ix.discover_repodocs(store)
    _commit(docs, "docs/specs/s.md", "same\n")
    docs.push(remote=REMOTE, path="docs/specs/s.md", commit="working", content="draft\n")
    assert _dirty(store) == set()


def test_visibility_only_repush_makes_the_indexed_row_private(store, docs):
    _commit(docs, "docs/specs/s.md", "osprey\n")
    ix.Indexer(store).run_once()
    assert _rows(store, "SELECT visibility FROM repodoc_fts")[0][0] == "public"
    _commit(docs, "docs/specs/s.md", "osprey\n", vis="private")
    ix.discover_repodocs(store)
    assert _rows(store, "SELECT visibility FROM repodoc_fts")[0][0] == "private"


def test_hard_deleted_doc_is_dropped_from_the_index(store, docs):
    _commit(docs, "docs/specs/s.md", "grebe\n")
    ix.Indexer(store).run_once()
    with store.connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM repodoc_versions")
        conn.execute("DELETE FROM repodocs")
    ix.discover_repodocs(store)
    assert _rows(store, "SELECT count(*) FROM repodoc_fts")[0][0] == 0


def test_a_new_indexer_sweeps_every_doc_once(store, docs):
    _commit(docs, "docs/specs/s.md", "kestrel\n")
    ix.discover_repodocs(store)
    with store.connect() as conn:  # drift no write path explains
        conn.execute("DELETE FROM repodoc_index_queue")
    ix.Indexer(store).run_once()
    assert len(_queued(store)) == 1


def test_requeue_all_also_re_plans_every_doc(store, docs):
    _commit(docs, "docs/specs/s.md", "plover\n")
    ix.discover_repodocs(store)
    with store.connect() as conn:
        conn.execute("DELETE FROM repodoc_index_queue")
    ix.requeue_all(store)
    assert ix.discover_repodocs(store) == 1


def test_reopen_replaces_a_stale_trigger(tmp_path):
    data = tmp_path / "data"
    Store(data)
    conn = sqlite3.connect(data / "library.db")
    conn.execute("DROP TRIGGER repodoc_drafts_insert_dirty")
    conn.execute(
        "CREATE TRIGGER repodoc_drafts_insert_dirty AFTER INSERT ON repodoc_drafts"
        " BEGIN SELECT 1; END"
    )
    conn.commit()
    conn.close()
    store = Store(data)
    sql = _rows(store, "SELECT sql FROM sqlite_master WHERE name = 'repodoc_drafts_insert_dirty'")[
        0
    ][0]
    assert "repodoc_dirty" in sql
