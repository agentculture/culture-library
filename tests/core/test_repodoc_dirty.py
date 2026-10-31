"""Repo doc discovery reads only changed docs (#39): an idle tick never scans ``repodocs``.

On Postgres (plan task t13) the marks are plpgsql triggers from
``core/migrations/0002_indexer_triggers_and_split_words.sql``.
"""

from __future__ import annotations

import re
import threading
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from culture_library.core import Store
from culture_library.core import indexer as ix
from culture_library.core import migrations
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
    with store.reader() as conn:
        return conn.execute(sql, params).fetchall()


def _queued(store):
    return {
        r[0]: r[1] for r in _rows(store, "SELECT doc_id, shown_sha256 FROM repodoc_index_queue")
    }


class _Trace:
    """Collect every SQL statement run through a psycopg connection's ``execute``."""

    def __init__(self, store, monkeypatch):
        self.sql: list[str] = []
        real = psycopg.Connection.execute

        def traced(conn, query, *args, **kwargs):
            self.sql.append(str(query))
            return real(conn, query, *args, **kwargs)

        monkeypatch.setattr(psycopg.Connection, "execute", traced)

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
    assert all("d.id = ANY(%s)" in s for s in planned)  # only the claimed marks
    assert any(s.startswith("DELETE FROM repodoc_dirty RETURNING") for s in trace.sql)


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


@pytest.fixture
def fresh_db(pg_admin_dsn):
    """An empty C-collation database of its own (not the worker's shared one)."""
    name = f"t13_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_admin_dsn, autocommit=True) as conn:
        conn.execute(
            f"CREATE DATABASE {name} TEMPLATE template0 LOCALE_PROVIDER libc"
            " LC_COLLATE 'C' LC_CTYPE 'C'"
        )
    yield make_conninfo(pg_admin_dsn, dbname=name)
    with psycopg.connect(pg_admin_dsn, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def test_upgrade_seeds_every_existing_doc_once(tmp_path, fresh_db):
    before = [m for m in migrations.load() if m.version < 2]  # the schema before the triggers
    with psycopg.connect(fresh_db) as conn:
        migrations.migrate(conn, before)
        for n in range(3):  # docs written while no trigger marked anything
            conn.execute(
                "INSERT INTO repodocs (repo, path, kind, created_at, updated_at)"
                " VALUES ('r', %s, 'spec', 't', 't')",
                (f"docs/specs/s{n}.md",),
            )
        conn.commit()
    store = Store(tmp_path / "data", dsn=fresh_db)  # applies 0002, which seeds the marks
    assert len(_dirty(store)) == 3
    with store.connect() as conn:
        conn.execute("DELETE FROM repodoc_dirty")
    Store(tmp_path / "data", dsn=fresh_db)  # a later open applies nothing and never re-seeds
    assert _dirty(store) == set()


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
    assert _rows(store, "SELECT visibility FROM repodoc_text")[0][0] == "public"
    _commit(docs, "docs/specs/s.md", "osprey\n", vis="private")
    ix.discover_repodocs(store)
    assert _rows(store, "SELECT visibility FROM repodoc_text")[0][0] == "private"


def test_hard_deleted_doc_is_dropped_from_the_index(store, docs):
    _commit(docs, "docs/specs/s.md", "grebe\n")
    ix.Indexer(store).run_once()
    with store.connect() as conn:  # no foreign key from the marks: a deleted doc still marks
        conn.execute("DELETE FROM repodoc_drafts")
        conn.execute("DELETE FROM repodoc_versions")
        conn.execute("DELETE FROM repodocs")
    assert _rows(store, "SELECT count(*) FROM repodoc_dirty")[0][0] == 1
    ix.discover_repodocs(store)
    assert _rows(store, "SELECT count(*) FROM repodoc_text")[0][0] == 0
    assert _rows(store, "SELECT count(*) FROM repodoc_dirty")[0][0] == 0


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


_WHEN_COLUMN = re.compile(r"\(?old\.(\w+) IS DISTINCT FROM new\.(\w+)\)?", re.I)


def test_triggers_watch_exactly_the_columns_discovery_reads(store):
    """The UPDATE triggers' WHEN clauses name indexer.DIRTY_SOURCES' columns, and every
    source table marks on INSERT, UPDATE and DELETE."""
    rows = _rows(
        store,
        "SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t"
        " JOIN pg_class c ON c.oid = t.tgrelid WHERE NOT t.tgisinternal"
        " AND pg_get_triggerdef(t.oid) LIKE %s",
        ("%repodoc_mark_dirty%",),
    )
    by_table: dict[str, list[str]] = {}
    for table, _name, sql in rows:
        by_table.setdefault(table, []).append(sql)
    assert set(by_table) == {table for table, _, _ in ix.DIRTY_SOURCES}
    for table, _column, watched in ix.DIRTY_SOURCES:
        defs = by_table[table]
        events = " ".join(re.search(r"AFTER (.*?) ON", d).group(1) for d in defs)
        assert {"INSERT", "UPDATE", "DELETE"} <= set(events.replace(" OR ", " ").split())
        update = [d for d in defs if "AFTER UPDATE" in d]
        assert len(update) == 1, defs
        cols = {a for a, b in _WHEN_COLUMN.findall(update[0]) if a == b}
        assert cols == set(watched), (table, update[0])
        assert all("FOR EACH ROW" in d for d in defs)


def test_shown_sql_reads_only_watched_columns_of_the_marked_tables():
    """A column _SHOWN_SQL reads from a marked table must be watched (or never change)."""
    unwatched_ok = {"id", "doc_id", "committed_at", "removed_at"}  # keys and watched elsewhere
    aliases = {
        "d": "repodocs",
        "dr": "repodoc_drafts",
        "cv": "repodoc_versions",
        "nv": "repodoc_versions",
    }
    watched = {table: set(cols) for table, _, cols in ix.DIRTY_SOURCES}
    for alias, column in re.findall(r"\b(d|dr|cv|nv)\.(\w+)", ix._SHOWN_SQL):
        table = aliases[alias]
        assert column in watched[table] or column in unwatched_ok, (alias, column)


def test_a_mark_held_by_an_open_writer_is_not_lost(store, docs):
    """Discovery claims marks and reads docs in two statements: a push that has marked a doc
    but not committed makes the claim wait, so its change is planned, never cleared unseen."""
    _commit(docs, "docs/specs/s.md", "first\n")
    ix.discover_repodocs(store)
    _commit(docs, "docs/specs/s.md", "first\n", vis="private")  # a committed mark is waiting
    writer = psycopg.connect(store._conninfo)
    try:
        # an open push of new bytes: its trigger touches the existing mark (and locks it)
        writer.execute(
            "UPDATE repodoc_versions SET sha256 = %s WHERE doc_id = (SELECT id FROM repodocs)",
            ("f" * 64,),
        )
        result = []
        t = threading.Thread(target=lambda: result.append(ix.discover_repodocs(store)))
        t.start()
        t.join(0.5)
        assert t.is_alive()  # the claim waits for the writer
        writer.commit()
        t.join(10)
        assert result == [1]
    finally:
        writer.close()
    assert _queued(store) == {_rows(store, "SELECT id FROM repodocs")[0][0]: "f" * 64}
