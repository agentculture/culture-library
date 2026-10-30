"""Radar axes, group means, run comparison and per-source breakdown: pure, stdlib only.

Works on the dicts :mod:`culture_library.core.eval_store` returns: a run (``get_run``) and its
results (``list_run_samples``). A *scored run* is the pair ``(run, samples)``. Every axis value
is in [0, 1], higher is better; ``None`` means the run did not score that metric (drawn as a
gap, never as 0).

Axis order follows the design canvas: found at 1, 3, 5, MRR, judge precision, judge recall,
no errors, then the newer suites. Precision at k is stored but kept off the radar (its ceiling
is 1/k for single-reference questions).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from culture_library.core.eval_metrics import item_aware_metrics

DEFAULT_BUDGET_S = 1.0  # park v2: latency budget for the speed axis, a parameter everywhere
AXES = (
    "found_at_1",
    "found_at_3",
    "found_at_5",
    "mrr",
    "judge_precision",
    "judge_recall",
    "no_errors",
    "latency_in_budget",
    "no_duplicates",
    "fresh",
)
_ID_AXES = ("found_at_1", "found_at_3", "found_at_5", "mrr")
# Metrics of the built-in suites: each is mapped to an axis above or kept off the radar on
# purpose (precision at k, depth 10 and nDCG beyond the canvas, raw latencies, counts).
HANDLED_METRICS = frozenset(
    {
        "found_at_1",
        "found_at_3",
        "found_at_5",
        "found_at_10",
        "precision_at_k",
        "mrr",
        "ndcg",
        "context_precision",
        "context_recall",
        "duplicate_rate",
        "distinct_sources",
        "latency_p50_ms",
        "latency_p95_ms",
        "within_budget",
        "timeout_rate",
        "error_rate",
        "fresh_count",
        "stale_count",
        "fresh_unknown_count",
        "fresh_na_count",
        "fresh_share",
    }
)
# Extra axes from suites registered after the built-ins: ``[(metric, direction), ...]`` of
# 0-1 metrics, supplied by the eval package (core never imports it). A new suite then reaches
# the radar with no edit here: "higher" metrics as they are, "lower" ones as 1 - value.
_axis_source: Callable[[], Sequence[tuple[str, str]]] | None = None


def set_axis_source(source: Callable[[], Sequence[tuple[str, str]]] | None) -> None:
    """Install the callable that lists extra (registered-suite) axes; ``None`` removes it."""
    global _axis_source
    _axis_source = source


def _extra_axes() -> list[tuple[str, str]]:
    if _axis_source is None:
        return []
    return [(m, d) for m, d in _axis_source() if m not in HANDLED_METRICS and m not in AXES]


def axis_names() -> tuple[str, ...]:
    """The canvas axes followed by any registered extra-suite axes, in that order."""
    return AXES + tuple(m for m, _ in _extra_axes())


_JUDGE = {"judge_precision": "context_precision", "judge_recall": "context_recall"}
_DEFAULT_K = 5

Run = Mapping[str, Any]
Result = Mapping[str, Any]
Scored = tuple[Run, Sequence[Result]]


def _mean(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _k(run: Run, samples: Sequence[Result]) -> int:
    k = (run.get("config_json") or {}).get("k")
    if isinstance(k, int) and k > 0:
        return k
    return max((len(s.get("retrieved_chunk_shas") or []) for s in samples), default=0) or _DEFAULT_K


def _question_metrics(sample: Result, k: int, item_id: Any = None) -> dict[str, Any]:
    """ID metrics for one result, item-aware when the snapshot allows it.

    Every reference belongs to the set's own item, so when the result stored the
    retrieved item ids (one per rank) a passage only counts when item and sha both
    match; identical text in another book shares a sha and must not be credited.

    A result with no reference snapshot (``None``, as a backfill leaves a run whose set was
    re-anchored since) cannot be re-scored: its references are gone. It keeps the ID scores
    the run stored when it was scored, and a metric the run never stored stays ``None``.
    """
    ids = sample.get("retrieved_item_ids")
    refs = sample.get("reference_chunk_shas")
    out = item_aware_metrics(
        list(sample.get("retrieved_chunk_shas") or []),
        ids if isinstance(ids, list) else None,
        list(refs or []),
        sample.get("item_id", item_id),
        k,
    )
    if refs is None and sample.get("question") is not None:
        stored = sample.get("scores_json") or {}
        out = {key: stored.get(key) if key != "depth" else val for key, val in out.items()}
    return out


def _failed_ids(run: Run) -> set[Any]:
    """Sample ids whose retrieval failed, from the run's ``config.errors``."""
    errors = (run.get("config_json") or {}).get("errors") or []
    return {e.get("sample_id") for e in errors if isinstance(e, Mapping)}


def _failed(sample: Result, failed_ids: set[Any]) -> bool:
    """True when the runner recorded this question's retrieval as an error."""
    if sample.get("sample_id") in failed_ids:
        return True
    return ((sample.get("scores_json") or {}).get("error_rate") or 0.0) >= 1.0


def _judge(run: Run, samples: Sequence[Result], key: str, whole_run: bool) -> float | None:
    if whole_run:
        for col in ("aggregates_json", "partial_aggregates_json"):
            agg = run.get(col)
            if agg and agg.get(key) is not None:
                return agg[key]
    return _mean((s.get("scores_json") or {}).get(key) for s in samples)


def _axes(
    run: Run, samples: Sequence[Result], budget_s: float, whole_run: bool
) -> dict[str, float | None]:
    k = _k(run, samples)
    # For a whole run a failed retrieval already counts against no_errors, so its empty ranks
    # are left out (as the run's partial aggregates do). A breakdown slice has no no_errors
    # axis, so there a failure still scores as a miss and the weakest list keeps showing it.
    failed = _failed_ids(run) if whole_run else None
    rows = [
        _question_metrics(s, k, run.get("item_id"))
        for s in samples
        if failed is None or not _failed(s, failed)
    ]
    out: dict[str, float | None] = {a: _mean(r[a] for r in rows) for a in _ID_AXES}
    for axis, key in _JUDGE.items():
        out[axis] = _judge(run, samples, key, whole_run)
    err = run.get("error_rate") if whole_run else None
    out["no_errors"] = None if err is None else 1.0 - err
    lat = [s["latency_ms"] for s in samples if s.get("latency_ms") is not None]
    out["latency_in_budget"] = (
        sum(1 for ms in lat if ms <= budget_s * 1000) / len(lat) if lat else None
    )
    dup = _judge(run, samples, "duplicate_rate", whole_run)
    out["no_duplicates"] = None if dup is None else 1.0 - dup
    out["fresh"] = _judge(run, samples, "fresh_share", whole_run)
    for metric, direction in _extra_axes():
        v = _judge(run, samples, metric, whole_run)
        out[metric] = v if v is None or direction != "lower" else 1.0 - v
    return {a: out[a] for a in axis_names()}


def axes(
    run: Run, samples: Sequence[Result] | None = None, *, budget_s: float = DEFAULT_BUDGET_S
) -> dict[str, float | None]:
    """One 0-1, higher-is-better value per axis for a run (``None`` = not measured).

    ``samples`` defaults to ``run["samples"]``. ID axes are recomputed from the stored ranks, so
    runs without stored metrics still get them; the judge axes come from the run's aggregates
    (or partial aggregates), falling back to the mean of per-question scores. ``no_errors`` is
    ``1 - error_rate``; ``latency_in_budget`` is the share of retrievals within ``budget_s``.
    """
    rows = run.get("samples", []) if samples is None else samples
    return _axes(run, rows, budget_s, True)


def group(runs: Sequence[Scored], *, budget_s: float = DEFAULT_BUDGET_S) -> dict[str, Any]:
    """Mean shape over documents: each document's axes first, then the mean of documents.

    A 4-question newspaper weighs the same as a 20-question book, so every document reports
    its question count. A document that did not score an axis is left out of that axis' mean.
    """
    docs = []
    for run, samples in runs:
        docs.append(
            {
                "run_id": run.get("id"),
                "set_id": run.get("set_id"),
                "questions": len(samples),
                "axes": axes(run, samples, budget_s=budget_s),
            }
        )
    mean = {a: _mean(d["axes"].get(a) for d in docs) for a in axis_names()}
    return {"axes": mean, "documents": docs}


def _suites(run: Run) -> tuple[set[str], dict[str, Any]]:
    names: set[str] = set()
    versions: dict[str, Any] = dict((run.get("config_json") or {}).get("suite_versions") or {})
    for entry in run.get("suites_json") or []:
        if isinstance(entry, Mapping):
            names.add(entry["name"])
            if "version" in entry:
                versions[entry["name"]] = entry["version"]
        else:
            names.add(str(entry))
    return names, versions


def _hard_reasons(a: Run, b: Run, names_a: set[str], names_b: set[str]) -> list[str]:
    reasons: list[str] = []
    if names_a != names_b:
        reasons.append(
            f"different suites: {sorted(names_a) or 'none'} vs {sorted(names_b) or 'none'}"
        )
    judge_a = (a.get("config_json") or {}).get("judge_model")
    judge_b = (b.get("config_json") or {}).get("judge_model")
    if judge_a != judge_b:
        reasons.append(f"different judge: {judge_a!r} vs {judge_b!r}")
    return reasons


def _set_warnings(a: Run, b: Run) -> list[str]:
    if a.get("set_id") != b.get("set_id"):
        return [f"different set: {a.get('set_id')} vs {b.get('set_id')}"]
    if a.get("set_version") != b.get("set_version"):
        return [f"set version differs: {a.get('set_version')} vs {b.get('set_version')}"]
    return []


def _soft_warnings(a: Run, b: Run, ver_a: dict[str, Any], ver_b: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if ver_a != ver_b:
        warnings.append(f"suite version differs: {ver_a} vs {ver_b}")
    warnings.extend(_set_warnings(a, b))
    for label, run in (("before", a), ("after", b)):
        if run.get("status") != "complete":
            warnings.append(f"{label} run is {run.get('status')}, not complete (partial scores)")
    ka = (a.get("config_json") or {}).get("k")
    kb = (b.get("config_json") or {}).get("k")
    if ka != kb:
        warnings.append(f"depth differs: k={ka} vs k={kb}; MRR is over min(10, k) ranks")
    return warnings


def _checks(a: Run, b: Run) -> dict[str, Any]:
    names_a, ver_a = _suites(a)
    names_b, ver_b = _suites(b)
    reasons = _hard_reasons(a, b, names_a, names_b)
    warnings = [] if reasons else _soft_warnings(a, b, ver_a, ver_b)
    return {"comparable": not reasons, "reasons": reasons, "warnings": warnings}


def _qkey(sample: Result) -> Any:
    return sample.get("question") or ("sample", sample.get("sample_id"))


def _summary(sample: Result, k: int, item_id: Any = None) -> dict[str, float | None]:
    m = _question_metrics(sample, k, item_id)
    return {a: m[a] for a in _ID_AXES}


def _delta(before: Mapping[str, float | None], after: Mapping[str, float | None]) -> float:
    diffs = [
        after[a] - before[a] for a in _ID_AXES if before[a] is not None and after[a] is not None
    ]
    return sum(diffs) / len(diffs) if diffs else 0.0


def compare(before: Scored, after: Scored, *, budget_s: float = DEFAULT_BUDGET_S) -> dict[str, Any]:
    """Checks first, then (only when comparable) both shapes, deltas and changed questions.

    Different suites or judge: ``comparable`` is False and nothing else is returned. A different
    suite version, set, set version, a non-complete run or a different depth/k is a warning.
    ``changed_questions`` pairs questions by text (or sample id when a snapshot lacks it) and
    lists them worst first by the mean change over the ID axes.
    """
    (run_a, rows_a), (run_b, rows_b) = before, after
    checks = _checks(run_a, run_b)
    if not checks["comparable"]:
        return {"checks": checks}
    ax_a = axes(run_a, rows_a, budget_s=budget_s)
    ax_b = axes(run_b, rows_b, budget_s=budget_s)
    deltas = {
        a: None if ax_a[a] is None or ax_b[a] is None else ax_b[a] - ax_a[a] for a in axis_names()
    }
    k_a, k_b = _k(run_a, rows_a), _k(run_b, rows_b)
    prior = {_qkey(s): s for s in rows_a}
    failed_a, failed_b = _failed_ids(run_a), _failed_ids(run_b)
    changed = []
    for s in rows_b:
        old = prior.get(_qkey(s))
        # A question that failed on either side is not on the ID axes (it moves no_errors),
        # so it is not listed as an ID change either.
        if old is None or _failed(old, failed_a) or _failed(s, failed_b):
            continue
        sb = _summary(old, k_a, run_a.get("item_id"))
        sa = _summary(s, k_b, run_b.get("item_id"))
        if sb != sa:
            changed.append(
                {"question": s.get("question"), "before": sb, "after": sa, "delta": _delta(sb, sa)}
            )
    changed.sort(key=lambda c: c["delta"])
    return {
        "checks": checks,
        "before": ax_a,
        "after": ax_b,
        "deltas": deltas,
        "changed_questions": changed,
    }


def breakdown(
    run: Run,
    samples: Sequence[Result] | None = None,
    set_samples: Sequence[Mapping[str, Any]] | None = None,
    *,
    budget_s: float = DEFAULT_BUDGET_S,
) -> dict[str, Any]:
    """Aggregates per item and, for newspaper questions, per source URL (weakest first).

    Item comes from each result's ``item_id`` (else the run's); ``set_samples`` is
    ``eval_store.list_samples`` output, whose ``source_urls`` are joined to results by sample id
    (a question with several URLs counts toward each). Without ``set_samples`` there are no
    source rows. Rows report ``questions`` and ``axes`` (ID and judge axes; run-level axes such as
    ``no_errors`` are not measured per slice).
    """
    rows = run.get("samples", []) if samples is None else samples
    items: dict[Any, list[Result]] = {}
    urls: dict[str, list[Result]] = {}
    by_id = {s["id"]: s.get("source_urls") or [] for s in set_samples or []}
    for r in rows:
        items.setdefault(r.get("item_id", run.get("item_id")), []).append(r)
        for url in by_id.get(r.get("sample_id"), []):
            urls.setdefault(url, []).append(r)

    def rollup(key: str, group_: Any, part: Sequence[Result]) -> dict[str, Any]:
        return {key: group_, "questions": len(part), "axes": _axes(run, part, budget_s, False)}

    def weakest(row: dict[str, Any]) -> tuple[float, float]:
        ax = row["axes"]
        return (ax["found_at_5"] if ax["found_at_5"] is not None else 1.0, ax["mrr"] or 0.0)

    return {
        "items": [rollup("item_id", i, p) for i, p in items.items()],
        "sources": sorted((rollup("url", u, p) for u, p in urls.items()), key=weakest),
    }


__all__ = [
    "AXES",
    "DEFAULT_BUDGET_S",
    "HANDLED_METRICS",
    "axes",
    "axis_names",
    "breakdown",
    "compare",
    "group",
    "set_axis_source",
]
