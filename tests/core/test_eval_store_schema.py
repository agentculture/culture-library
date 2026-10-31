"""eval_store on Postgres: columns, run snapshots, upsert, jsonb handling, backfill, races."""

from __future__ import annotations

import math
import threading

import pytest

from culture_library.core import Store, eval_store
from culture_library.core.errors import ValidationError

NEW_RUN_COLS = {"partial_aggregates_json", "error_rate", "suites_json", "running"}
NEW_SAMPLE_COLS = {
    "question",
    "reference_chunk_shas",
    "set_version",
    "retrieved_item_ids",
    "latency_ms",
    "freshness",
}


def _cols(store, table: str) -> set[str]:
    with store.reader() as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)
        ).fetchall()
    return {r[0] for r in rows}


def test_fresh_db_has_new_columns(tmp_path):
    store = Store(tmp_path)
    assert NEW_RUN_COLS <= _cols(store, "eval_runs")
    assert NEW_SAMPLE_COLS <= _cols(store, "eval_run_samples")


def _legacy(store):
    """A set of three samples and an old run (no snapshot fields) of the first two."""
    item = store.add_item(b"body", kind="document", title="T", format="md").item
    with store.connect() as conn:
        set_id = eval_store.create_set(
            conn, item.id, item.sha256, chunker={}, adapter="a", adapter_version="1", generator={}
        )
        eval_store.add_samples(
            conn,
            set_id,
            [
                {
                    "question": f"q{n}",
                    "reference_answer": "ans",
                    "reference_chunk_shas": [f"ref{n}"],
                }
                for n in (1, 2)
            ],
        )
        run_id = eval_store.create_run(
            conn, set_id, status="complete", config={}, total=2, completed=2, aggregates={"m": 1}
        )
        for sample in eval_store.list_samples(conn, set_id):
            eval_store.add_run_sample(
                conn, run_id, sample["id"], retrieved_chunk_shas=["x"], scores={"s": 1}
            )
    return set_id, run_id


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
        assert run["running"] is True
        with pytest.raises(ValidationError):
            eval_store.create_run(
                conn, set_id, status="partial", config={}, total=1, completed=1, aggregates={"a": 1}
            )
        old = eval_store.create_run(
            conn, set_id, status="complete", config={}, total=1, completed=1
        )
        assert eval_store.get_run(conn, old)["running"] is False
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
        assert old["question"] is None
        assert old["latency_ms"] is None
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
    store = Store(tmp_path)
    set_id, run_id = _legacy(store)
    with store.connect() as conn:
        assert eval_store.backfill_snapshots(conn) == 2
        rows = eval_store.list_run_samples(conn, run_id)
        assert [(r["question"], r["reference_chunk_shas"]) for r in rows] == [
            ("q1", ["ref1"]),
            ("q2", ["ref2"]),
        ]
        assert eval_store.backfill_snapshots(conn) == 0


def test_backfill_does_not_copy_references_from_a_newer_set_version(tmp_path):
    """After a re-anchor the current shas are not what the old run was scored against."""
    store = Store(tmp_path)
    set_id, run_id = _legacy(store)
    with store.connect() as conn:
        conn.execute("UPDATE eval_sets SET version = 2 WHERE id = %s", (set_id,))
        assert eval_store.backfill_snapshots(conn) == 2
        rows = eval_store.list_run_samples(conn, run_id)
        assert [r["question"] for r in rows] == ["q1", "q2"]  # questions never change
        assert [r["reference_chunk_shas"] for r in rows] == [None, None]
        assert [r["set_version"] for r in rows] == [None, None]


def test_backfill_records_the_set_version_it_copied_from(tmp_path):
    store = Store(tmp_path)
    set_id, run_id = _legacy(store)
    with store.connect() as conn:
        eval_store.backfill_snapshots(conn)
        assert [r["set_version"] for r in eval_store.list_run_samples(conn, run_id)] == [1, 1]


def _unguarded_backfill(conn):
    """What 0.18.0/0.18.1 left: the current question and references, no set_version."""
    conn.execute(
        "UPDATE eval_run_samples SET "
        "question = (SELECT question FROM eval_samples WHERE id = sample_id), "
        "reference_chunk_shas = (SELECT reference_chunk_shas FROM eval_samples "
        "WHERE id = sample_id)"
    )


def test_backfill_repairs_an_older_backfill_from_a_newer_set_version(tmp_path):
    store = Store(tmp_path)
    set_id, run_id = _legacy(store)
    with store.connect() as conn:
        conn.execute("UPDATE eval_sets SET version = 2 WHERE id = %s", (set_id,))
        _unguarded_backfill(conn)
        assert eval_store.backfill_snapshots(conn) == 0  # repairs are not counted
        rows = eval_store.list_run_samples(conn, run_id)
        assert [r["question"] for r in rows] == ["q1", "q2"]
        assert [r["reference_chunk_shas"] for r in rows] == [None, None]
        eval_store.backfill_snapshots(conn)  # idempotent
        assert [r["reference_chunk_shas"] for r in eval_store.list_run_samples(conn, run_id)] == [
            None,
            None,
        ]


def test_backfill_stamps_the_version_on_an_older_same_version_backfill(tmp_path):
    store = Store(tmp_path)
    set_id, run_id = _legacy(store)
    with store.connect() as conn:
        _unguarded_backfill(conn)
        eval_store.backfill_snapshots(conn)
        rows = eval_store.list_run_samples(conn, run_id)
        assert [(r["reference_chunk_shas"], r["set_version"]) for r in rows] == [
            (["ref1"], 1),
            (["ref2"], 1),
        ]


def test_add_run_sample_twice_upserts_one_row(tmp_path):
    store = Store(tmp_path)
    set_id, run_id = _legacy(store)
    with store.connect() as conn:
        sid = eval_store.list_samples(conn, set_id)[0]["id"]
        eval_store.add_run_sample(conn, run_id, sid, retrieved_chunk_shas=["new"], scores={"s": 2})
        rows = [r for r in eval_store.list_run_samples(conn, run_id) if r["sample_id"] == sid]
        assert len(rows) == 1
        assert rows[0]["retrieved_chunk_shas"] == ["new"]
        assert rows[0]["scores_json"] == {"s": 2}


def test_non_finite_numbers_are_stored_as_null(tmp_path):
    store = Store(tmp_path)
    set_id, run_id = _legacy(store)
    with store.connect() as conn:
        sid = eval_store.list_samples(conn, set_id)[0]["id"]
        eval_store.add_run_sample(
            conn,
            run_id,
            sid,
            retrieved_chunk_shas=[],
            scores={"a": math.nan, "b": {"c": [math.inf, 0.5]}, "d": -math.inf},
            latency_ms=math.nan,
        )
        row = [r for r in eval_store.list_run_samples(conn, run_id) if r["sample_id"] == sid][0]
        assert row["scores_json"] == {"a": None, "b": {"c": [None, 0.5]}, "d": None}
        assert row["latency_ms"] is None
        fin = eval_store.create_run(
            conn,
            set_id,
            status="partial",
            config={"x": math.nan},
            total=1,
            completed=0,
            partial_aggregates={"m": math.inf},
            error_rate=math.nan,
        )
        run = eval_store.get_run(conn, fin)
        assert run["config_json"] == {"x": None}
        assert run["partial_aggregates_json"] == {"m": None}
        assert run["error_rate"] is None


def test_json_columns_come_back_with_sorted_keys(tmp_path):
    store = Store(tmp_path)
    set_id, run_id = _legacy(store)
    scores = {"zz": 1, "b": 2, "mmm": {"y": 1, "aaaa": 2, "c": 3}, "a": 4}
    with store.connect() as conn:
        sid = eval_store.list_samples(conn, set_id)[0]["id"]
        eval_store.add_run_sample(conn, run_id, sid, retrieved_chunk_shas=[], scores=scores)
        row = [r for r in eval_store.list_run_samples(conn, run_id) if r["sample_id"] == sid][0]
        assert list(row["scores_json"]) == ["a", "b", "mmm", "zz"]  # not jsonb's length order
        assert list(row["scores_json"]["mmm"]) == ["aaaa", "c", "y"]


def _running(store, set_id):
    with store.connect() as conn:
        return eval_store.create_run(
            conn, set_id, status="partial", config={}, total=2, completed=0, running=True
        )


def test_close_running_runs_marks_partial_or_failed(tmp_path):
    store = Store(tmp_path)
    set_id, _ = _legacy(store)
    empty, with_results = _running(store, set_id), _running(store, set_id)
    with store.connect() as conn:
        sid = eval_store.list_samples(conn, set_id)[0]["id"]
        eval_store.add_run_sample(conn, with_results, sid, retrieved_chunk_shas=[], scores={"s": 1})
        assert eval_store.close_running_runs(conn, "restart") == [empty, with_results]
        a, b = eval_store.get_run(conn, empty), eval_store.get_run(conn, with_results)
        assert (a["status"], a["running"]) == ("failed", False)
        assert (b["status"], b["running"], b["completed"]) == ("partial", False, 1)
        assert b["partial_aggregates_json"] == {"s": 1.0}
        assert b["config_json"]["closed_reason"] == "restart"
        assert eval_store.running_runs(conn) == []


def test_close_running_runs_leaves_a_run_being_started_alone(tmp_path):
    store = Store(tmp_path)
    set_id, _ = _legacy(store)
    old = _running(store, set_id)
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    new: list[int] = []

    def start_run():  # a run start whose transaction is still open while the close runs
        with store.connect() as conn:
            new.append(
                eval_store.create_run(
                    conn, set_id, status="partial", config={}, total=2, completed=0, running=True
                )
            )
            started.set()
            release.wait(10)
        done.set()

    worker = threading.Thread(target=start_run)
    worker.start()
    assert started.wait(10)
    with store.connect() as conn:
        assert eval_store.close_running_runs(conn, "restart") == [old]
    release.set()
    assert done.wait(10)
    worker.join()
    with store.reader() as conn:
        assert eval_store.running_runs(conn) == new  # the new run survived, still running


def test_close_running_runs_does_not_block_on_or_overwrite_a_finishing_run(tmp_path):
    store = Store(tmp_path)
    set_id, _ = _legacy(store)
    busy, idle = _running(store, set_id), _running(store, set_id)
    locked, release = threading.Event(), threading.Event()

    def finish():  # a worker holding the run's row while it writes its final result
        with store.connect() as conn:
            eval_store.finish_run(conn, busy, status="complete", config={}, completed=2)
            locked.set()
            release.wait(10)

    worker = threading.Thread(target=finish)
    worker.start()
    assert locked.wait(10)
    with store.connect() as conn:
        assert eval_store.close_running_runs(conn, "restart") == [idle]  # skipped, not waited for
    release.set()
    worker.join()
    with store.reader() as conn:
        assert eval_store.get_run(conn, busy)["status"] == "complete"
        assert eval_store.get_run(conn, busy)["config_json"] == {}


def test_lock_set_runs_serialises_concurrent_starts(tmp_path):
    store = Store(tmp_path)
    set_id, _ = _legacy(store)
    barrier = threading.Barrier(4)
    started: list[int | None] = []

    def start():
        barrier.wait(10)
        with store.connect() as conn:
            eval_store.lock_set_runs(conn, set_id)
            if eval_store.running_runs(conn, set_id):
                started.append(None)
                return
            started.append(
                eval_store.create_run(
                    conn, set_id, status="partial", config={}, total=1, completed=0, running=True
                )
            )

    threads = [threading.Thread(target=start) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([s for s in started if s is not None]) == 1
    assert started.count(None) == 3
