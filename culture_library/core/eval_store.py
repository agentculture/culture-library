"""RAG evaluation storage: sets, samples, runs and per-sample run results.

An *eval set* belongs to one item (``eval_sets.item_id``, ON DELETE CASCADE); its samples,
runs and run results cascade through the set, so deleting or purging an item leaves nothing
behind. ``version`` counts regenerations of a set's samples; two runs are
:func:`comparable` only when they ran the same set at the same version. A run's
``aggregates_json`` is set only when its status is ``complete``.

List functions take the same ``include_private`` / ``owner`` arguments as
:mod:`culture_library.core.library` (via :func:`uploaders.visible_sql`). Archived items are
hidden. Stdlib only, no network.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from . import uploaders
from .errors import NotFoundError, ValidationError
from .store import now, register_migration, register_schema

RUN_STATUSES = ("complete", "partial", "failed")

register_schema("""
CREATE TABLE IF NOT EXISTS eval_sets (
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
CREATE INDEX IF NOT EXISTS eval_sets_item ON eval_sets(item_id);
CREATE TABLE IF NOT EXISTS eval_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id INTEGER NOT NULL REFERENCES eval_sets(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    reference_answer TEXT NOT NULL,
    reference_chunk_shas TEXT NOT NULL DEFAULT '[]',
    reference_contexts TEXT NOT NULL DEFAULT '[]',
    source_urls TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS eval_samples_set ON eval_samples(set_id);
CREATE TABLE IF NOT EXISTS eval_runs (
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
CREATE INDEX IF NOT EXISTS eval_runs_set ON eval_runs(set_id);
CREATE TABLE IF NOT EXISTS eval_run_samples (
    run_id INTEGER NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
    sample_id INTEGER NOT NULL,
    retrieved_chunk_shas TEXT NOT NULL DEFAULT '[]',
    scores_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, sample_id)
)
""")

# Columns added after 0.17.2, per table. Existing databases get them with ALTER TABLE ADD COLUMN
# (never a table rebuild), so ids, rows and sqlite_sequence are untouched.
_ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "eval_runs": (
        ("partial_aggregates_json", "TEXT"),
        ("error_rate", "REAL"),
        ("suites_json", "TEXT"),
        ("running", "INTEGER NOT NULL DEFAULT 0"),
    ),
    "eval_run_samples": (
        ("question", "TEXT"),
        ("reference_chunk_shas", "TEXT"),
        ("set_version", "INTEGER"),
        ("retrieved_item_ids", "TEXT"),
        ("latency_ms", "REAL"),
        ("freshness", "TEXT"),
    ),
}


def ensure_columns(conn: sqlite3.Connection) -> None:
    """Add any missing column from :data:`_ADDED_COLUMNS` (idempotent)."""
    for table, columns in _ADDED_COLUMNS.items():
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}  # nosec B608
        for name, decl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")  # nosec B608


register_migration(ensure_columns)

_SET_COLS = (
    "id, item_id, item_sha256, chunker_json, adapter, adapter_version, generator_json, "
    "version, created_at"
)
_RUN_COLS = (
    "id, set_id, set_version, status, config_json, total, completed, aggregates_json, created_at, "
    "partial_aggregates_json, error_rate, suites_json, running"
)
_RUN_SAMPLE_COLS = (
    "run_id, sample_id, retrieved_chunk_shas, scores_json, question, reference_chunk_shas, "
    "set_version, retrieved_item_ids, latency_ms, freshness"
)
_JSON_RUN_SAMPLE = (
    "retrieved_chunk_shas",
    "scores_json",
    "reference_chunk_shas",
    "retrieved_item_ids",
    "freshness",
)
_JSON_SET = ("chunker_json", "generator_json")
_JSON_RUN = ("config_json", "aggregates_json", "partial_aggregates_json", "suites_json")


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _decode(row: sqlite3.Row | tuple, cols: str, json_cols: Sequence[str]) -> dict[str, Any]:
    out = dict(zip((c.strip() for c in cols.split(",")), tuple(row), strict=True))
    for col in json_cols:
        raw = out[col]
        out[col] = None if raw is None else json.loads(raw)
    return out


def create_set(
    conn: sqlite3.Connection,
    item_id: int,
    item_sha256: str,
    *,
    chunker: Any,
    adapter: str,
    adapter_version: str,
    generator: Any,
) -> int:
    """Insert an eval set (version 1) for an item; return its id."""
    cur = conn.execute(
        "INSERT INTO eval_sets (item_id, item_sha256, chunker_json, adapter, adapter_version, "
        "generator_json, version, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
        (item_id, item_sha256, _dumps(chunker), adapter, adapter_version, _dumps(generator), now()),
    )
    return int(cur.lastrowid)


def get_set(conn: sqlite3.Connection, set_id: int) -> dict[str, Any]:
    sql = f"SELECT {_SET_COLS} FROM eval_sets WHERE id = ?"  # nosec B608
    row = conn.execute(sql, (set_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"eval set {set_id} not found")
    return _decode(row, _SET_COLS, _JSON_SET)


def list_sets(
    conn: sqlite3.Connection,
    *,
    item_id: int | None = None,
    include_private: bool = False,
    owner: str | None = None,
) -> list[dict[str, Any]]:
    """Eval sets whose item the caller may see (same rule as the library), oldest first."""
    vis, params = uploaders.visible_sql(
        include_private, owner, visibility="i.visibility", item_id="i.id"
    )
    sql = (
        "SELECT " + ", ".join(f"s.{c.strip()}" for c in _SET_COLS.split(",")) + " "  # nosec B608
        "FROM eval_sets s JOIN items i ON i.id = s.item_id "
        f"WHERE i.archived_at IS NULL AND {vis}"
    )
    if item_id is not None:
        sql += " AND s.item_id = ?"
        params = [*params, item_id]
    rows = conn.execute(sql + " ORDER BY s.id", params).fetchall()
    return [_decode(r, _SET_COLS, _JSON_SET) for r in rows]


def replace_samples(
    conn: sqlite3.Connection, set_id: int, samples: Sequence[Mapping[str, Any]]
) -> int:
    """Replace a set's samples with ``samples``, bump its version; return the new version.

    Each sample mapping has ``question`` and ``reference_answer`` and optionally
    ``reference_chunk_shas``, ``reference_contexts`` and ``source_urls`` (lists).
    """
    get_set(conn, set_id)
    _check_samples(samples)
    conn.execute("DELETE FROM eval_samples WHERE set_id = ?", (set_id,))
    _insert_samples(conn, set_id, samples)
    conn.execute("UPDATE eval_sets SET version = version + 1 WHERE id = ?", (set_id,))
    return int(conn.execute("SELECT version FROM eval_sets WHERE id = ?", (set_id,)).fetchone()[0])


def _check_samples(samples: Sequence[Mapping[str, Any]]) -> None:
    for sample in samples:
        if not sample.get("question") or "reference_answer" not in sample:
            raise ValidationError("a sample needs a question and a reference_answer")


def _insert_samples(
    conn: sqlite3.Connection, set_id: int, samples: Sequence[Mapping[str, Any]]
) -> None:
    for sample in samples:
        conn.execute(
            "INSERT INTO eval_samples (set_id, question, reference_answer, reference_chunk_shas, "
            "reference_contexts, source_urls) VALUES (?, ?, ?, ?, ?, ?)",
            (
                set_id,
                sample["question"],
                sample["reference_answer"],
                _dumps(list(sample.get("reference_chunk_shas", []))),
                _dumps(list(sample.get("reference_contexts", []))),
                _dumps(list(sample.get("source_urls", []))),
            ),
        )


def add_samples(
    conn: sqlite3.Connection, set_id: int, samples: Sequence[Mapping[str, Any]]
) -> None:
    """Append samples to a set without changing its version (initial generation)."""
    get_set(conn, set_id)
    _check_samples(samples)
    _insert_samples(conn, set_id, samples)


def list_samples(conn: sqlite3.Connection, set_id: int) -> list[dict[str, Any]]:
    cols = (
        "id, set_id, question, reference_answer, reference_chunk_shas, "
        "reference_contexts, source_urls"
    )
    sql = f"SELECT {cols} FROM eval_samples WHERE set_id = ? ORDER BY id"  # nosec B608
    rows = conn.execute(sql, (set_id,)).fetchall()
    return [
        _decode(r, cols, ("reference_chunk_shas", "reference_contexts", "source_urls"))
        for r in rows
    ]


def create_run(
    conn: sqlite3.Connection,
    set_id: int,
    *,
    status: str,
    config: Any,
    total: int,
    completed: int,
    aggregates: Any = None,
    partial_aggregates: Any = None,
    error_rate: float | None = None,
    suites: Sequence[str] | None = None,
    running: bool = False,
) -> int:
    """Record a run of a set at the set's current version; return the run id.

    ``aggregates`` is only accepted for a ``complete`` run; a partial run keeps what it scored
    in ``partial_aggregates`` (with ``error_rate``).
    """
    if status not in RUN_STATUSES:
        raise ValidationError(f"run status must be one of {', '.join(RUN_STATUSES)}")
    if aggregates is not None and status != "complete":
        raise ValidationError("aggregates are only stored for a complete run")
    version = get_set(conn, set_id)["version"]
    cur = conn.execute(
        "INSERT INTO eval_runs (set_id, set_version, status, config_json, total, completed, "
        "aggregates_json, created_at, partial_aggregates_json, error_rate, suites_json, running) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            set_id,
            version,
            status,
            _dumps(config),
            total,
            completed,
            None if aggregates is None else _dumps(aggregates),
            now(),
            None if partial_aggregates is None else _dumps(partial_aggregates),
            error_rate,
            None if suites is None else _dumps(list(suites)),
            1 if running else 0,
        ),
    )
    return int(cur.lastrowid)


def add_run_sample(
    conn: sqlite3.Connection,
    run_id: int,
    sample_id: int,
    *,
    retrieved_chunk_shas: Sequence[str],
    scores: Mapping[str, Any],
    question: str | None = None,
    reference_chunk_shas: Sequence[str] | None = None,
    set_version: int | None = None,
    retrieved_item_ids: Sequence[int] | None = None,
    latency_ms: float | None = None,
    freshness: Any = None,
) -> None:
    """Store one sample's result; the optional fields are the run's per-question snapshot."""
    conn.execute(
        "INSERT OR REPLACE INTO eval_run_samples (run_id, sample_id, retrieved_chunk_shas, "
        "scores_json, question, reference_chunk_shas, set_version, retrieved_item_ids, "
        "latency_ms, freshness) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            sample_id,
            _dumps(list(retrieved_chunk_shas)),
            _dumps(dict(scores)),
            question,
            None if reference_chunk_shas is None else _dumps(list(reference_chunk_shas)),
            set_version,
            None if retrieved_item_ids is None else _dumps(list(retrieved_item_ids)),
            latency_ms,
            None if freshness is None else _dumps(freshness),
        ),
    )


def backfill_snapshots(conn: sqlite3.Connection) -> int:
    """Copy question and reference shas from ``eval_samples`` into run results lacking them.

    Idempotent: a result that already has a question is left alone. A result whose sample was
    deleted (a regenerated set) cannot be filled. Returns how many results were filled.
    """
    cur = conn.execute(
        "UPDATE eval_run_samples SET "
        "question = (SELECT question FROM eval_samples WHERE id = eval_run_samples.sample_id), "
        "reference_chunk_shas = (SELECT reference_chunk_shas FROM eval_samples "
        "WHERE id = eval_run_samples.sample_id) "
        "WHERE question IS NULL AND EXISTS "
        "(SELECT 1 FROM eval_samples WHERE id = eval_run_samples.sample_id)"
    )
    return int(cur.rowcount)


def get_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    sql = f"SELECT {_RUN_COLS} FROM eval_runs WHERE id = ?"  # nosec B608
    row = conn.execute(sql, (run_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"eval run {run_id} not found")
    return _decode(row, _RUN_COLS, _JSON_RUN)


def list_runs(
    conn: sqlite3.Connection,
    *,
    set_id: int | None = None,
    include_private: bool = False,
    owner: str | None = None,
) -> list[dict[str, Any]]:
    """Runs whose set's item the caller may see, oldest first."""
    vis, params = uploaders.visible_sql(
        include_private, owner, visibility="i.visibility", item_id="i.id"
    )
    sql = (
        "SELECT " + ", ".join(f"r.{c.strip()}" for c in _RUN_COLS.split(",")) + " "  # nosec B608
        "FROM eval_runs r JOIN eval_sets s ON s.id = r.set_id JOIN items i ON i.id = s.item_id "
        f"WHERE i.archived_at IS NULL AND {vis}"
    )
    if set_id is not None:
        sql += " AND r.set_id = ?"
        params = [*params, set_id]
    rows = conn.execute(sql + " ORDER BY r.id", params).fetchall()
    return [_decode(r, _RUN_COLS, _JSON_RUN) for r in rows]


def list_run_samples(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    sql = (
        f"SELECT {_RUN_SAMPLE_COLS} FROM eval_run_samples "  # nosec B608
        "WHERE run_id = ? ORDER BY sample_id"
    )
    rows = conn.execute(sql, (run_id,)).fetchall()
    return [_decode(r, _RUN_SAMPLE_COLS, _JSON_RUN_SAMPLE) for r in rows]


def comparable(run_a: Mapping[str, Any], run_b: Mapping[str, Any]) -> bool:
    """True only when both runs are of the same eval set at the same set version."""
    return run_a["set_id"] == run_b["set_id"] and run_a["set_version"] == run_b["set_version"]


__all__ = [
    "RUN_STATUSES",
    "add_run_sample",
    "add_samples",
    "backfill_snapshots",
    "comparable",
    "create_run",
    "create_set",
    "ensure_columns",
    "get_run",
    "get_set",
    "list_run_samples",
    "list_runs",
    "list_samples",
    "list_sets",
    "replace_samples",
]
