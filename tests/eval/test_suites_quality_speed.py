"""The result_quality and speed suites, on fixture results."""

from __future__ import annotations

import json

import pytest

from culture_library.eval import suites
from culture_library.eval.models import Passage, Sample
from culture_library.eval.suites.base import HIGHER, LOWER, SuiteContext
from culture_library.eval.suites.result_quality import ResultQualitySuite
from culture_library.eval.suites.speed import SpeedSuite


def _p(item_id, text, sha=None):
    return Passage(item_id=item_id, chunk_sha=sha or f"sha-{text}", text=text)


def _ctx(retrieved, k=5, **kw):
    samples = [Sample(question=f"q{i}") for i in range(len(retrieved))]
    return SuiteContext(samples, retrieved, k=k, **kw)


def _metric(suite, name):
    return next(m for m in suite.metrics if m.name == name)


def test_registered_with_definitions_range_direction():
    cat = {s["name"]: s for s in json.loads(json.dumps(suites.catalog()))}
    assert {"result_quality", "speed"} <= set(cat)
    for name in ("result_quality", "speed"):
        assert cat[name]["version"] == "1"
        assert cat[name]["needs_judge"] is False
        for m in cat[name]["metrics"]:
            assert m["definition"] and m["direction"] in ("higher", "lower")
    rq = suites.get("result_quality")
    assert _metric(rq, "duplicate_rate").direction == LOWER
    assert _metric(rq, "distinct_sources").direction == HIGHER
    assert "exact" in _metric(rq, "duplicate_rate").definition
    sp = suites.get("speed")
    assert _metric(sp, "latency_p50_ms").direction == LOWER
    assert _metric(sp, "within_budget").direction == HIGHER
    assert _metric(sp, "error_rate").direction == LOWER
    assert "1000" in _metric(sp, "within_budget").definition


def test_duplicate_rate_and_distinct_sources():
    same = [_p(1, "a"), _p(2, "a"), _p(2, "b"), _p(3, "c"), _p(3, "c")]
    ctx = _ctx([same, [_p(1, "x"), _p(2, "y")]])
    out = ResultQualitySuite().score(ctx)
    assert out.per_sample[0] == {"duplicate_rate": 2 / 5, "distinct_sources": 3}
    assert out.per_sample[1] == {"duplicate_rate": 0.0, "distinct_sources": 2}
    assert out.aggregate["duplicate_rate"] == pytest.approx(0.2)
    assert out.aggregate["distinct_sources"] == 2.5


def test_only_top_k_counts_and_sha_fallback_without_text():
    top = [_p(1, "a"), _p(2, "b"), _p(3, "c")]
    ctx = _ctx([top + [_p(4, "a")]], k=3)
    assert ResultQualitySuite().score(ctx).per_sample[0]["duplicate_rate"] == 0.0
    nt = [Passage(1, "s1"), Passage(2, "s1"), Passage(3, "s2")]
    out = ResultQualitySuite().score(_ctx([nt]))
    assert out.per_sample[0]["duplicate_rate"] == pytest.approx(1 / 3)


def test_empty_retrieval_is_not_measured():
    out = ResultQualitySuite().score(_ctx([[], [_p(1, "a")]]))
    assert out.per_sample[0] == {"duplicate_rate": None, "distinct_sources": None}
    assert out.aggregate["duplicate_rate"] == 0.0
    allempty = ResultQualitySuite().score(_ctx([[]]))
    assert allempty.aggregate["distinct_sources"] is None


def test_speed_percentiles_budget_and_errors():
    lat = [100.0, 200.0, 300.0, 400.0, 2000.0, None, None]
    err = [None, None, None, None, None, "HTTP 500", "request timed out"]
    ctx = _ctx([[_p(1, "a")]] * 5 + [[], []], latencies_ms=lat, errors=err)
    out = SpeedSuite().score(ctx)
    a = out.aggregate
    assert a["latency_p50_ms"] == 300.0
    assert a["latency_p95_ms"] == pytest.approx(1680.0)
    assert a["within_budget"] == 4 / 5
    assert a["error_rate"] == 2 / 7
    assert a["timeout_rate"] == 1 / 7
    assert out.per_sample[5]["error_rate"] == 1.0
    assert out.per_sample[5]["timeout_rate"] == 0.0
    assert out.per_sample[6]["timeout_rate"] == 1.0
    assert out.per_sample[4]["within_budget"] == 0.0
    assert out.per_sample[6]["within_budget"] is None


def test_budget_is_configurable_and_in_definition():
    ctx = _ctx([[_p(1, "a")]] * 2, latencies_ms=[100.0, 600.0], errors=[None, None])
    suite = SpeedSuite(budget_ms=500)
    assert suite.score(ctx).aggregate["within_budget"] == 0.5
    assert "500" in _metric(suite, "within_budget").definition
    with pytest.raises(ValueError):
        SpeedSuite(budget_ms=0)


def test_unrecorded_latency_and_errors_are_none_not_zero():
    out = SpeedSuite().score(_ctx([[_p(1, "a")]]))
    assert all(v is None for v in out.aggregate.values())
    assert out.skipped is None
    only_lat = SpeedSuite().score(_ctx([[_p(1, "a")]], latencies_ms=[50.0]))
    assert only_lat.aggregate["latency_p50_ms"] == 50.0
    assert only_lat.aggregate["error_rate"] is None


def test_all_failed_leaves_latency_undefined_error_rate_one():
    ctx = _ctx([[]], latencies_ms=[None], errors=["connection refused"])
    a = SpeedSuite().score(ctx).aggregate
    assert a["error_rate"] == 1.0 and a["latency_p50_ms"] is None
