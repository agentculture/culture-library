"""eval_store additive schema: ensure-columns on open, run snapshots, backfill."""

from __future__ import annotations

import sqlite3

import pytest

from culture_library.core import Store, eval_store
from culture_library.core.errors import ValidationError
from tests.core.test_store_migration import _make_old_db

# The eval DDL exactly as 0.17.2 shipped it (before the additive columns).
OLD_EVAL_DDL = """
CREATE TABLE eval_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    item_sha256 TEXT NOT NULL,
    chunker_json TEXT NOT NULL,
    adapter TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    generator_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX eval_sets_item ON eval_sets(item_id);
CREATE TABLE eval_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id INTEGER NOT NULL REFERENCES eval_sets(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    reference_answer TEXT NOT NULL,
    reference_chunk_shas TEXT NOT NULL DEFAULT '[]',
    reference_contexts TEXT NOT NULL DEFAULT '[]',
    source_urls TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX eval_samples_set ON eval_samples(set_id);
CREATE TABLE eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id INTEGER NOT NULL REFERENCES eval_sets(id) ON DELETE CASCADE,
    set_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK (status IN ('complete', 'partial', 'failed')),
    config_json TEXT NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    aggregates_json TEXT,
    created_at TEXT NOT NULL,
    CHECK (aggregates_json IS NULL OR status = 'complete')
);
CREATE INDEX eval_runs_set ON eval_runs(set_id);
CREATE TABLE eval_run_samples (
    run_id INTEGER NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
    sample_id INTEGER NOT NULL,
    retrieved_chunk_shas TEXT NOT NULL DEFAULT '[]',
    scores_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, sample_id)
)
"""
TABLES = ("eval_sets", "eval_samples", "eval_runs", "eval_run_samples")
NEW_RUN_COLS = {"partial_aggregates_json", "error_rate", "suites_json", "running"}
NEW_SAMPLE_COLS = {
    "question",
    "reference_chunk_shas",
    "set_version",
    "retrieved_item_ids",
    "latency_ms",
    "freshness",
}


def _cols(db, table: str) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def _state(db) -> dict:
    conn = sqlite3.connect(db)
    try:
        return {
            "rootpage": {
                r[0]: r[1]
                for r in conn.execute(
                    "SELECT name, rootpage FROM sqlite_master "
                    "WHERE type='table' AND name LIKE 'eval_%'"
                )
            },
            "seq": dict(
                conn.execute("SELECT name, seq FROM sqlite_sequence WHERE name LIKE 'eval_%'")
            ),
            "sets": conn.execute("SELECT * FROM eval_sets ORDER BY id").fetchall(),
            "samples": conn.execute("SELECT * FROM eval_samples ORDER BY id").fetchall(),
            "runs": conn.execute("SELECT * FROM eval_runs ORDER BY id").fetchall(),
            "run_samples": conn.execute(
                "SELECT run_id, sample_id, retrieved_chunk_shas, scores_json "
                "FROM eval_run_samples ORDER BY run_id, sample_id"
            ).fetchall(),
        }
    finally:
        conn.close()


def _old_db_with_eval(data_dir):
    db = _make_old_db(data_dir)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(OLD_EVAL_DDL)
    conn.execute(
        "INSERT INTO eval_sets (item_id, item_sha256, chunker_json, adapter, adapter_version, "
        "generator_json, version, created_at) VALUES (1, 's', '{}', 'a', '1', '{}', 1, 't')"
    )
    for n in range(1, 4):
        conn.execute(
            "INSERT INTO eval_samples (set_id, question, reference_answer, reference_chunk_shas)"
            " VALUES (1, ?, 'ans', ?)",
            (f"q{n}", f'["ref{n}"]'),
        )
    conn.execute("DELETE FROM eval_samples WHERE id = 3")  # sqlite_sequence now 3, max id 2
    conn.execute(
        "INSERT INTO eval_runs (set_id, status, config_json, total, completed, aggregates_json,"
        " created_at) VALUES (1, 'complete', '{}', 2, 2, '{\"m\": 1}', 't')"
    )
    for sid in (1, 2):
        conn.execute(
            "INSERT INTO eval_run_samples (run_id, sample_id, retrieved_chunk_shas, scores_json)"
            " VALUES (1, ?, '[\"x\"]', '{\"s\": 1}')",
            (sid,),
        )
    conn.commit()
    conn.close()
    return db


def test_open_old_db_adds_columns_without_rebuild(tmp_path):
    data = tmp_path / "data"
    db = _old_db_with_eval(data)
    before = _state(db)
    assert before["seq"]["eval_samples"] == 3
    assert not NEW_RUN_COLS & set(_cols(db, "eval_runs"))

    Store(data)

    assert NEW_RUN_COLS <= set(_cols(db, "eval_runs"))
    assert NEW_SAMPLE_COLS <= set(_cols(db, "eval_run_samples"))
    after = _state(db)
    assert after["rootpage"] == before["rootpage"]  # no table was rebuilt
    assert after["seq"] == before["seq"]
    assert after["sets"] == before["sets"]
    assert after["samples"] == before["samples"]
    assert after["run_samples"] == before["run_samples"]
    assert [r[0] for r in after["runs"]] == [1]
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT running, error_rate FROM eval_runs").fetchone() == (0, None)
    conn.close()
    Store(data)  # reopening is a no-op
    assert _state(db) == after


def test_fresh_db_has_new_columns(tmp_path):
    Store(tmp_path)
    assert NEW_RUN_COLS <= set(_cols(tmp_path / "library.db", "eval_runs"))
    assert NEW_SAMPLE_COLS <= set(_cols(tmp_path / "library.db", "eval_run_samples"))


def _set(store):
    item = store.add_item(b"body", kind="document", title="T", format="md").item
    with store.connect() as conn:
        set_id = eval_store.create_set(
            conn, item.id, item.sha256, chunker={}, adapter="a", adapter_version="1", generator={}
        )
        eval_store.add_samples(
            conn,
            set_id,
            [{"question": "q1", "reference_answer": "a", "reference_chunk_shas": ["r1"]}],
        )
    return set_id


def test_partial_run_stores_partial_aggregates_and_error_rate(tmp_path):
    store = Store(tmp_path)
    set_id = _set(store)
    with store.connect() as conn:
        run_id = eval_store.create_run(
            conn,
            set_id,
            status="partial",
            config={},
            total=20,
            completed=19,
            partial_aggregates={"recall": 0.5},
            error_rate=0.05,
            suites=["id"],
            running=True,
        )
        run = eval_store.get_run(conn, run_id)
        assert run["aggregates_json"] is None
        assert run["partial_aggregates_json"] == {"recall": 0.5}
        assert run["error_rate"] == 0.05
        assert run["suites_json"] == ["id"]
        assert run["running"] == 1
        with pytest.raises(ValidationError):
            eval_store.create_run(
                conn, set_id, status="partial", config={}, total=1, completed=1, aggregates={"a": 1}
            )
        old = eval_store.create_run(
            conn, set_id, status="complete", config={}, total=1, completed=1
        )
        assert eval_store.get_run(conn, old)["running"] == 0
        assert eval_store.get_run(conn, old)["partial_aggregates_json"] is None


def test_add_run_sample_snapshot_fields_and_old_callers(tmp_path):
    store = Store(tmp_path)
    set_id = _set(store)
    with store.connect() as conn:
        run_id = eval_store.create_run(
            conn, set_id, status="complete", config={}, total=1, completed=1
        )
        sid = eval_store.list_samples(conn, set_id)[0]["id"]
        eval_store.add_run_sample(conn, run_id, sid, retrieved_chunk_shas=["c"], scores={"s": 1})
        old = eval_store.list_run_samples(conn, run_id)[0]
        assert old["question"] is None and old["latency_ms"] is None
        eval_store.add_run_sample(
            conn,
            run_id,
            sid,
            retrieved_chunk_shas=["c"],
            scores={"s": 1},
            question="q1",
            reference_chunk_shas=["r1"],
            set_version=1,
            retrieved_item_ids=[7, 7],
            latency_ms=12.5,
            freshness={"changed": False},
        )
        row = eval_store.list_run_samples(conn, run_id)[0]
        assert row["question"] == "q1"
        assert row["reference_chunk_shas"] == ["r1"]
        assert row["set_version"] == 1
        assert row["retrieved_item_ids"] == [7, 7]
        assert row["latency_ms"] == 12.5
        assert row["freshness"] == {"changed": False}


def test_backfill_snapshots_fills_missing_and_is_idempotent(tmp_path):
    data = tmp_path / "data"
    db = _old_db_with_eval(data)
    store = Store(data)
    with store.connect() as conn:
        assert eval_store.backfill_snapshots(conn) == 2
        rows = eval_store.list_run_samples(conn, 1)
        assert [(r["question"], r["reference_chunk_shas"]) for r in rows] == [
            ("q1", ["ref1"]),
            ("q2", ["ref2"]),
        ]
        assert eval_store.backfill_snapshots(conn) == 0
    assert db.exists()
