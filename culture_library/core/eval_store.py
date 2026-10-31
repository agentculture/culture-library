"""RAG evaluation storage: sets, samples, runs and per-sample run results.

An *eval set* belongs to one item (``eval_sets.item_id``, ON DELETE CASCADE); its samples,
runs and run results cascade through the set, so deleting or purging an item leaves nothing
behind. ``version`` counts regenerations of a set's samples; two runs are
:func:`comparable` only when they ran the same set at the same version. A run's
``aggregates_json`` is set only when its status is ``complete``.

List functions take the same ``include_private`` / ``owner`` arguments as
:mod:`culture_library.core.library` (the rule in :func:`_visible`). Archived items are
hidden. Stdlib only, no network.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .errors import NotFoundError, ValidationError
from .store import now

RUN_STATUSES = ("complete", "partial", "failed")

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


def _clean(value: Any) -> Any:
    """``value`` with NaN and infinity turned into None: jsonb rejects them, JSON has none."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _finite(value: float | None) -> float | None:
    return value if value is None or math.isfinite(value) else None


def _json(value: Any) -> Any:
    """A jsonb parameter for ``value`` (None stays SQL NULL); non-finite floats become null."""
    from psycopg.types.json import Jsonb

    return None if value is None else Jsonb(_clean(value))


def _sorted(value: Any) -> Any:
    """``value`` with object keys sorted at every depth.

    jsonb orders keys by length, then bytes; the SQLite store sorted them alphabetically and
    API responses (and the contract tests) were written against that, so reads restore it.
    """
    if isinstance(value, dict):
        return {k: _sorted(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_sorted(v) for v in value]
    return value


def _decode(row: Sequence[Any], cols: str, json_cols: Sequence[str]) -> dict[str, Any]:
    out = dict(zip((c.strip() for c in cols.split(",")), tuple(row), strict=True))
    for col in json_cols:
        out[col] = _sorted(out[col])
    return out


def _visible(include_private: bool, owner: str | None, item: str) -> tuple[str, list[str]]:
    """The library's visibility rule for the item alias ``item``, as ``(condition, params)``:
    public, or private access, or uploaded by ``owner`` (table ``item_uploaders``)."""
    if include_private:
        return "TRUE", []
    if owner:
        owned = "IN (SELECT item_id FROM item_uploaders WHERE key_id = %s)"
        return f"({item}.visibility = 'public' OR {item}.id {owned})", [owner]
    return f"{item}.visibility = 'public'", []


def create_set(
    conn: Any,
    item_id: int,
    item_sha256: str,
    *,
    chunker: Any,
    adapter: str,
    adapter_version: str,
    generator: Any,
) -> int:
    """Insert an eval set (version 1) for an item; return its id."""
    row = conn.execute(
        "INSERT INTO eval_sets (item_id, item_sha256, chunker_json, adapter, adapter_version, "
        "generator_json, version, created_at) VALUES (%s, %s, %s, %s, %s, %s, 1, %s) RETURNING id",
        (item_id, item_sha256, _json(chunker), adapter, adapter_version, _json(generator), now()),
    ).fetchone()
    return int(row[0])


def get_set(conn: Any, set_id: int) -> dict[str, Any]:
    sql = f"SELECT {_SET_COLS} FROM eval_sets WHERE id = %s"  # nosec B608
    row = conn.execute(sql, (set_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"eval set {set_id} not found")
    return _decode(row, _SET_COLS, _JSON_SET)


def list_sets(
    conn: Any,
    *,
    item_id: int | None = None,
    include_private: bool = False,
    owner: str | None = None,
) -> list[dict[str, Any]]:
    """Eval sets whose item the caller may see (same rule as the library), oldest first."""
    vis, params = _visible(include_private, owner, "i")
    sql = (
        "SELECT " + ", ".join(f"s.{c.strip()}" for c in _SET_COLS.split(",")) + " "  # nosec B608
        "FROM eval_sets s JOIN items i ON i.id = s.item_id "
        f"WHERE i.archived_at IS NULL AND {vis}"
    )
    if item_id is not None:
        sql += " AND s.item_id = %s"
        params = [*params, item_id]
    rows = conn.execute(sql + " ORDER BY s.id", params).fetchall()
    return [_decode(r, _SET_COLS, _JSON_SET) for r in rows]


def replace_samples(conn: Any, set_id: int, samples: Sequence[Mapping[str, Any]]) -> int:
    """Replace a set's samples with ``samples``, bump its version; return the new version.

    Each sample mapping has ``question`` and ``reference_answer`` and optionally
    ``reference_chunk_shas``, ``reference_contexts`` and ``source_urls`` (lists).
    """
    get_set(conn, set_id)
    _check_samples(samples)
    conn.execute("DELETE FROM eval_samples WHERE set_id = %s", (set_id,))
    _insert_samples(conn, set_id, samples)
    conn.execute("UPDATE eval_sets SET version = version + 1 WHERE id = %s", (set_id,))
    return int(conn.execute("SELECT version FROM eval_sets WHERE id = %s", (set_id,)).fetchone()[0])


def _check_samples(samples: Sequence[Mapping[str, Any]]) -> None:
    for sample in samples:
        if not sample.get("question") or "reference_answer" not in sample:
            raise ValidationError("a sample needs a question and a reference_answer")


def _insert_samples(conn: Any, set_id: int, samples: Sequence[Mapping[str, Any]]) -> None:
    for sample in samples:
        conn.execute(
            "INSERT INTO eval_samples (set_id, question, reference_answer, reference_chunk_shas, "
            "reference_contexts, source_urls) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                set_id,
                sample["question"],
                sample["reference_answer"],
                _json(list(sample.get("reference_chunk_shas", []))),
                _json(list(sample.get("reference_contexts", []))),
                _json(list(sample.get("source_urls", []))),
            ),
        )


def add_samples(conn: Any, set_id: int, samples: Sequence[Mapping[str, Any]]) -> None:
    """Append samples to a set without changing its version (initial generation)."""
    get_set(conn, set_id)
    _check_samples(samples)
    _insert_samples(conn, set_id, samples)


def list_samples(conn: Any, set_id: int) -> list[dict[str, Any]]:
    cols = (
        "id, set_id, question, reference_answer, reference_chunk_shas, "
        "reference_contexts, source_urls"
    )
    sql = f"SELECT {cols} FROM eval_samples WHERE set_id = %s ORDER BY id"  # nosec B608
    rows = conn.execute(sql, (set_id,)).fetchall()
    return [
        _decode(r, cols, ("reference_chunk_shas", "reference_contexts", "source_urls"))
        for r in rows
    ]


def create_run(
    conn: Any,
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
    row = conn.execute(
        "INSERT INTO eval_runs (set_id, set_version, status, config_json, total, completed, "
        "aggregates_json, created_at, partial_aggregates_json, error_rate, suites_json, running) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            set_id,
            version,
            status,
            _json(config),
            total,
            completed,
            None if aggregates is None else _json(aggregates),
            now(),
            None if partial_aggregates is None else _json(partial_aggregates),
            _finite(error_rate),
            None if suites is None else _json(list(suites)),
            bool(running),
        ),
    ).fetchone()
    return int(row[0])


def add_run_sample(
    conn: Any,
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
        "INSERT INTO eval_run_samples (run_id, sample_id, retrieved_chunk_shas, "
        "scores_json, question, reference_chunk_shas, set_version, retrieved_item_ids, "
        "latency_ms, freshness) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (run_id, sample_id) DO UPDATE SET "
        "retrieved_chunk_shas = EXCLUDED.retrieved_chunk_shas, "
        "scores_json = EXCLUDED.scores_json, question = EXCLUDED.question, "
        "reference_chunk_shas = EXCLUDED.reference_chunk_shas, "
        "set_version = EXCLUDED.set_version, "
        "retrieved_item_ids = EXCLUDED.retrieved_item_ids, "
        "latency_ms = EXCLUDED.latency_ms, freshness = EXCLUDED.freshness",
        (
            run_id,
            sample_id,
            _json(list(retrieved_chunk_shas)),
            _json(dict(scores)),
            question,
            None if reference_chunk_shas is None else _json(list(reference_chunk_shas)),
            set_version,
            None if retrieved_item_ids is None else _json(list(retrieved_item_ids)),
            _finite(latency_ms),
            None if freshness is None else _json(freshness),
        ),
    )


def backfill_snapshots(conn: Any) -> int:
    """Copy question and reference shas from ``eval_samples`` into run results lacking them.

    Idempotent: a result that already has a question is left alone. A result whose sample was
    deleted (a regenerated set) cannot be filled. The question never changes, so it is always
    copied; the reference shas (and the result's ``set_version``) are copied only while the
    set is still at the version the run used, because a re-anchor since then rewrote them and
    the current ones would not match what the run was scored against. Rows an older,
    unguarded backfill filled (a question but no ``set_version``) are repaired the same way.
    Returns how many results were filled (repairs are not counted).
    """
    same_version = (
        "(SELECT r.set_version FROM eval_runs r WHERE r.id = eval_run_samples.run_id) = "
        "(SELECT st.version FROM eval_samples sm JOIN eval_sets st ON st.id = sm.set_id "
        "WHERE sm.id = eval_run_samples.sample_id)"
    )
    # 0.18.0/0.18.1 backfilled without the version guard and without set_version. Repair
    # those rows first: references copied from a newer set version are withdrawn (the radar
    # then keeps the run's stored scores), and same-version rows get their set_version.
    conn.execute(
        "UPDATE eval_run_samples SET "  # nosec B608 - fixed SQL, no user input
        f"reference_chunk_shas = CASE WHEN {same_version} THEN reference_chunk_shas "
        "ELSE NULL END, "
        f"set_version = CASE WHEN {same_version} THEN "
        "(SELECT r.set_version FROM eval_runs r WHERE r.id = eval_run_samples.run_id) "
        "ELSE NULL END "
        "WHERE question IS NOT NULL AND set_version IS NULL "
        "AND reference_chunk_shas IS NOT NULL AND EXISTS "
        "(SELECT 1 FROM eval_samples WHERE id = eval_run_samples.sample_id)"
    )
    cur = conn.execute(
        "UPDATE eval_run_samples SET "  # nosec B608 - fixed SQL, no user input
        "question = (SELECT question FROM eval_samples WHERE id = eval_run_samples.sample_id), "
        f"reference_chunk_shas = CASE WHEN {same_version} THEN "
        "(SELECT reference_chunk_shas FROM eval_samples "
        "WHERE id = eval_run_samples.sample_id) ELSE reference_chunk_shas END, "
        f"set_version = CASE WHEN {same_version} THEN "
        "(SELECT r.set_version FROM eval_runs r WHERE r.id = eval_run_samples.run_id) "
        "ELSE set_version END "
        "WHERE question IS NULL AND EXISTS "
        "(SELECT 1 FROM eval_samples WHERE id = eval_run_samples.sample_id)"
    )
    return int(cur.rowcount)


def get_run(conn: Any, run_id: int) -> dict[str, Any]:
    sql = f"SELECT {_RUN_COLS} FROM eval_runs WHERE id = %s"  # nosec B608
    row = conn.execute(sql, (run_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"eval run {run_id} not found")
    return _decode(row, _RUN_COLS, _JSON_RUN)


def get_runs(conn: Any, run_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """The runs that exist among ``run_ids``, by id, in one query (missing ids are absent)."""
    ids = [int(i) for i in run_ids]
    if not ids:
        return {}
    sql = f"SELECT {_RUN_COLS} FROM eval_runs WHERE id = ANY(%s)"  # nosec B608
    runs = [_decode(r, _RUN_COLS, _JSON_RUN) for r in conn.execute(sql, (ids,)).fetchall()]
    return {r["id"]: r for r in runs}


def list_runs_samples(conn: Any, run_ids: Sequence[int]) -> dict[int, list[dict[str, Any]]]:
    """``list_run_samples`` for several runs in one query, keyed by run id."""
    ids = [int(i) for i in run_ids]
    out: dict[int, list[dict[str, Any]]] = {i: [] for i in ids}
    if not ids:
        return out
    sql = (
        f"SELECT run_id, {_RUN_SAMPLE_COLS} FROM eval_run_samples "  # nosec B608
        "WHERE run_id = ANY(%s) ORDER BY run_id, sample_id"
    )
    for row in conn.execute(sql, (ids,)).fetchall():
        out[row[0]].append(_decode(row[1:], _RUN_SAMPLE_COLS, _JSON_RUN_SAMPLE))
    return out


def list_runs(
    conn: Any,
    *,
    set_id: int | None = None,
    include_private: bool = False,
    owner: str | None = None,
) -> list[dict[str, Any]]:
    """Runs whose set's item the caller may see, oldest first."""
    vis, params = _visible(include_private, owner, "i")
    sql = (
        "SELECT " + ", ".join(f"r.{c.strip()}" for c in _RUN_COLS.split(",")) + " "  # nosec B608
        "FROM eval_runs r JOIN eval_sets s ON s.id = r.set_id JOIN items i ON i.id = s.item_id "
        f"WHERE i.archived_at IS NULL AND {vis}"
    )
    if set_id is not None:
        sql += " AND r.set_id = %s"
        params = [*params, set_id]
    rows = conn.execute(sql + " ORDER BY r.id", params).fetchall()
    return [_decode(r, _RUN_COLS, _JSON_RUN) for r in rows]


def list_run_samples(conn: Any, run_id: int) -> list[dict[str, Any]]:
    sql = (
        f"SELECT {_RUN_SAMPLE_COLS} FROM eval_run_samples "  # nosec B608
        "WHERE run_id = %s ORDER BY sample_id"
    )
    rows = conn.execute(sql, (run_id,)).fetchall()
    return [_decode(r, _RUN_SAMPLE_COLS, _JSON_RUN_SAMPLE) for r in rows]


def running_runs(conn: Any, set_id: int | None = None) -> list[int]:
    """Ids of runs still marked running (optionally of one set), oldest first."""
    sql = "SELECT id FROM eval_runs WHERE running"
    params: list[Any] = []
    if set_id is not None:
        sql += " AND set_id = %s"
        params.append(set_id)
    return [int(r[0]) for r in conn.execute(sql + " ORDER BY id", params).fetchall()]


def lock_set_runs(conn: Any, set_id: int) -> None:
    """Serialise run starts for one set until the transaction ends.

    A start that checks :func:`running_runs` and then inserts must call this first; two
    concurrent starts for the same set then queue instead of both seeing no running run.
    """
    conn.execute("SELECT id FROM eval_sets WHERE id = %s FOR NO KEY UPDATE", (set_id,))


def set_run_progress(conn: Any, run_id: int, completed: int) -> None:
    """Record how many questions a running run has finished."""
    conn.execute("UPDATE eval_runs SET completed = %s WHERE id = %s", (int(completed), run_id))


def finish_run(
    conn: Any,
    run_id: int,
    *,
    status: str,
    config: Any,
    completed: int,
    aggregates: Any = None,
    partial_aggregates: Any = None,
    error_rate: float | None = None,
    suites: Sequence[Any] | None = None,
) -> None:
    """Close a running run with its final status and scores, and clear ``running``.

    Same rule as :func:`create_run`: ``aggregates`` only for a ``complete`` run.
    """
    if status not in RUN_STATUSES:
        raise ValidationError(f"run status must be one of {', '.join(RUN_STATUSES)}")
    if aggregates is not None and status != "complete":
        raise ValidationError("aggregates are only stored for a complete run")
    get_run(conn, run_id)
    conn.execute(
        "UPDATE eval_runs SET status = %s, config_json = %s, completed = %s, "
        "aggregates_json = %s, partial_aggregates_json = %s, error_rate = %s, "
        "suites_json = %s, running = false WHERE id = %s",
        (
            status,
            _json(config),
            int(completed),
            None if aggregates is None else _json(aggregates),
            None if partial_aggregates is None else _json(partial_aggregates),
            _finite(error_rate),
            None if suites is None else _json(list(suites)),
            run_id,
        ),
    )


def _score_means(results: Sequence[Mapping[str, Any]]) -> dict[str, float | None]:
    values: dict[str, list[float]] = {}
    for res in results:
        for key, val in (res.get("scores_json") or {}).items():
            bucket = values.setdefault(key, [])
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                bucket.append(float(val))
    return {k: (sum(v) / len(v) if v else None) for k, v in sorted(values.items())}


def close_running_runs(conn: Any, reason: str) -> list[int]:
    """Close every run still marked running (a server restart interrupted it).

    A run with stored per-question results becomes ``partial`` (``completed`` = those results,
    ``partial_aggregates`` = the mean of each score over them); one with none becomes
    ``failed``. ``config["closed_reason"]`` records ``reason``. Returns the closed run ids.

    Safe beside a concurrent start or finish: only runs already committed as running are
    candidates (a run another transaction is creating is not seen), each is row-locked
    before it is read, so a worker finishing it is waited for rather than overwritten, and a
    run whose row is locked by someone else right now is skipped, never blocked on.
    """
    sql = "SELECT id FROM eval_runs WHERE running ORDER BY id FOR UPDATE SKIP LOCKED"
    closed = [int(r[0]) for r in conn.execute(sql).fetchall()]
    for run_id in closed:
        run = get_run(conn, run_id)
        results = list_run_samples(conn, run_id)
        config = dict(run["config_json"] or {})
        config["closed_reason"] = reason
        finish_run(
            conn,
            run_id,
            status="partial" if results else "failed",
            config=config,
            completed=len(results),
            partial_aggregates=_score_means(results) if results else None,
            error_rate=run["error_rate"],
            suites=run["suites_json"],
        )
    return closed


def comparable(run_a: Mapping[str, Any], run_b: Mapping[str, Any]) -> bool:
    """True only when both runs are of the same eval set at the same set version."""
    return run_a["set_id"] == run_b["set_id"] and run_a["set_version"] == run_b["set_version"]


__all__ = [
    "RUN_STATUSES",
    "add_run_sample",
    "add_samples",
    "backfill_snapshots",
    "get_runs",
    "list_runs_samples",
    "close_running_runs",
    "comparable",
    "create_run",
    "create_set",
    "lock_set_runs",
    "finish_run",
    "get_run",
    "get_set",
    "list_run_samples",
    "list_runs",
    "list_samples",
    "list_sets",
    "replace_samples",
    "running_runs",
    "set_run_progress",
]
