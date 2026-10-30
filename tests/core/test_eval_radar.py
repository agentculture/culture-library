"""Radar axes, group means, compare checks and breakdowns (pure functions over store dicts)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from culture_library.core import eval_radar as er

FIXTURE = Path(__file__).parent / "fixtures" / "eval_runs_11_19.json"
ORDER = [
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
]


def _run(run_id=1, **kw):
    base = {
        "id": run_id,
        "set_id": 1,
        "set_version": 1,
        "status": "complete",
        "config_json": {"k": 5, "judge_model": "cortex"},
        "aggregates_json": None,
        "error_rate": 0.0,
        "suites_json": ["id"],
    }
    base.update(kw)
    return base


def _s(sid, retrieved, refs, q=None, latency=None, **kw):
    return {
        "sample_id": sid,
        "question": q or f"q{sid}",
        "retrieved_chunk_shas": retrieved,
        "reference_chunk_shas": refs,
        "scores_json": kw.pop("scores", {}),
        "latency_ms": latency,
        **kw,
    }


def _fixture_run(rid, aggs):
    samples = [
        _s(
            i,
            s["retrieved_chunk_shas"],
            s["reference_chunk_shas"],
        )
        for i, s in enumerate(json.loads(FIXTURE.read_text())[rid])
    ]
    run = _run(int(rid), aggregates_json=aggs)
    return run, samples


def test_axes_order_and_design_canvas_values():
    run, samples = _fixture_run("19", {"context_precision": 0.857, "context_recall": 0.975})
    ax = er.axes(run, samples)
    assert list(ax) == ORDER
    assert ax["found_at_1"] == pytest.approx(0.20)
    assert ax["found_at_3"] == pytest.approx(0.65)
    assert ax["found_at_5"] == pytest.approx(0.70)
    assert round(ax["mrr"], 3) == 0.435
    assert ax["judge_precision"] == pytest.approx(0.857)
    assert ax["judge_recall"] == pytest.approx(0.975)
    assert ax["no_errors"] == 1.0
    run11, s11 = _fixture_run("11", {"context_precision": 0.904, "context_recall": 0.95})
    a11 = er.axes(run11, s11)
    assert [round(a11[k], 3) for k in ORDER[:4]] == [0.45, 0.80, 0.85, 0.618]


def test_precision_at_k_is_excluded_and_values_in_unit_range():
    run, samples = _fixture_run("19", None)
    ax = er.axes(run, samples)
    assert not any("precision_at" in k for k in ax)
    assert all(v is None or 0 <= v <= 1 for v in ax.values())


def test_unscored_metrics_are_none_not_zero():
    run = _run(aggregates_json=None, error_rate=None)
    ax = er.axes(run, [_s(1, ["a"], ["a"])])
    assert ax["judge_precision"] is None
    assert ax["judge_recall"] is None
    assert ax["no_errors"] is None
    assert ax["latency_in_budget"] is None
    assert ax["found_at_1"] == 1.0


def test_depth_beyond_k_is_none():
    run = _run(config_json={"k": 3})
    ax = er.axes(run, [_s(1, ["a", "b", "c"], ["c"])])
    assert ax["found_at_5"] is None
    assert ax["found_at_3"] == 1.0


def test_lower_is_better_turned_around_and_partial_aggregates_used():
    run = _run(status="partial", error_rate=0.25, partial_aggregates_json={"context_recall": 0.5})
    ax = er.axes(run, [_s(1, ["a"], ["a"])])
    assert ax["no_errors"] == 0.75
    assert ax["judge_recall"] == 0.5


def test_latency_is_share_within_budget_and_budget_is_a_parameter():
    samples = [_s(i, ["a"], ["a"], latency=ms) for i, ms in enumerate([200, 900, 1000, 1500])]
    run = _run()
    assert er.axes(run, samples)["latency_in_budget"] == 0.75  # default 1.0 s, inclusive
    assert er.axes(run, samples, budget_s=0.5)["latency_in_budget"] == 0.25
    assert er.DEFAULT_BUDGET_S == 1.0


def test_group_is_mean_over_documents_with_question_counts():
    big = (_run(1, set_id=1), [_s(i, ["a"], ["a"]) for i in range(20)])  # found1 = 1.0
    small = (_run(2, set_id=2), [_s(i, ["x"], ["a"]) for i in range(4)])  # found1 = 0.0
    g = er.group([big, small])
    assert g["axes"]["found_at_1"] == pytest.approx(0.5)  # not 20/24
    assert [d["questions"] for d in g["documents"]] == [20, 4]
    assert g["documents"][0]["axes"]["found_at_1"] == 1.0
    assert g["axes"]["judge_precision"] is None


def test_compare_checks_come_first_and_block_on_suite_or_judge():
    a, b = _run(1), _run(2, suites_json=["id", "speed"])
    out = er.compare((a, [_s(1, ["a"], ["a"])]), (b, [_s(1, ["a"], ["a"])]))
    assert list(out)[0] == "checks"
    assert out["checks"]["comparable"] is False
    assert set(out) == {"checks"}  # no deltas
    j = er.compare(
        (_run(1), [_s(1, ["a"], ["a"])]),
        (_run(2, config_json={"k": 5, "judge_model": "other"}), [_s(1, ["a"], ["a"])]),
    )
    assert j["checks"]["comparable"] is False
    assert set(j) == {"checks"}


def test_compare_warnings_do_not_block():
    s = [_s(1, ["a"], ["a"])]
    base = er.compare((_run(1), s), (_run(2), s))
    assert base["checks"] == {"comparable": True, "reasons": [], "warnings": []}
    cases = {
        "set": _run(2, set_id=9),
        "set_version": _run(2, set_version=2),
        "partial": _run(2, status="partial"),
        "suite_version": _run(
            2, config_json={"k": 5, "judge_model": "cortex", "suite_versions": {"id": 2}}
        ),
        "depth": _run(2, config_json={"k": 3, "judge_model": "cortex"}),
    }
    for key, other in cases.items():
        out = er.compare((_run(1), s), (other, s))
        assert out["checks"]["comparable"] is True, key
        assert [w for w in out["checks"]["warnings"] if key.split("_")[0] in w.lower()], key
        assert "deltas" in out


def test_compare_deltas_and_changed_questions_worst_first():
    before = [
        _s(1, ["a"], ["a"], q="one"),
        _s(2, ["b", "a"], ["a"], q="two"),
        _s(3, ["a"], ["a"], q="same"),
        _s(4, ["x", "y"], ["a"], q="four"),
    ]
    after = [
        _s(1, ["x"], ["a"], q="one"),  # worse
        _s(2, ["a"], ["a"], q="two"),  # better
        _s(3, ["a"], ["a"], q="same"),
        _s(4, ["a", "y"], ["a"], q="four"),  # better, bigger jump
    ]
    out = er.compare((_run(1), before), (_run(2), after))
    assert out["deltas"]["found_at_1"] == pytest.approx(0.5 - 0.5 + 0.25)
    changed = out["changed_questions"]
    assert [c["question"] for c in changed] == ["one", "two", "four"]
    assert changed[0]["delta"] < 0 < changed[-1]["delta"]
    assert changed[0]["before"]["mrr"] == 1.0
    assert changed[0]["after"]["mrr"] == 0.0


def test_breakdown_per_item_and_per_source_url():
    run = _run()
    samples = [
        _s(1, ["a"], ["a"], item_id=10),
        _s(2, ["x"], ["a"], item_id=10),
        _s(3, ["a"], ["a"], item_id=11),
        _s(4, ["a"], ["a"], item_id=11),
    ]
    sources = [
        {"id": 1, "source_urls": ["http://u/1"]},
        {"id": 2, "source_urls": ["http://u/1", "http://u/2"]},
        {"id": 3, "source_urls": ["http://u/2"]},
        {"id": 4, "source_urls": []},
    ]
    out = er.breakdown(run, samples, sources)
    items = {i["item_id"]: i for i in out["items"]}
    assert items[10]["questions"] == 2
    assert items[10]["axes"]["found_at_1"] == 0.5
    assert items[11]["axes"]["found_at_1"] == 1.0
    urls = {u["url"]: u for u in out["sources"]}
    assert urls["http://u/1"]["questions"] == 2
    assert urls["http://u/2"]["questions"] == 2
    assert urls["http://u/1"]["axes"]["found_at_1"] == 0.5
    assert out["sources"][0]["url"] == "http://u/1"  # weakest first
    assert er.breakdown(run, samples)["sources"] == []


def test_axes_item_aware_when_retrieved_item_ids_snapshotted():
    """A shared sha from another book is not credited once item ids are stored (c42)."""
    from culture_library.core.eval_radar import axes

    run = {"id": 1, "item_id": 68, "config_json": {"k": 5}, "error_rate": 0.0}
    other_book = {
        "sample_id": 1,
        "retrieved_chunk_shas": ["shared", "b", "c", "d", "e"],
        "retrieved_item_ids": [70, 68, 68, 68, 68],
        "reference_chunk_shas": ["shared"],
    }
    own_book = dict(other_book, sample_id=2, retrieved_item_ids=[68, 68, 68, 68, 68])
    assert axes(run, [other_book])["found_at_1"] == 0.0
    assert axes(run, [own_book])["found_at_1"] == 1.0
    legacy = {k: v for k, v in other_book.items() if k != "retrieved_item_ids"}
    assert axes(run, [legacy])["found_at_1"] == 1.0  # sha-only fallback for old runs


def test_axes_result_quality_and_freshness_from_suite_scores():
    """New suites add axes after the canvas seven: 1 - duplicate rate, and fresh share."""
    from culture_library.core.eval_radar import axes

    run = {"id": 1, "config_json": {"k": 5}, "aggregates_json": {"duplicate_rate": 0.2}}
    samples = [
        {"sample_id": 1, "scores_json": {"fresh_share": 1.0}},
        {"sample_id": 2, "scores_json": {"fresh_share": 0.0}},
        {"sample_id": 3, "scores_json": {"fresh_share": None}},
    ]
    ax = axes(run, samples)
    assert ax["no_duplicates"] == 0.8
    assert ax["fresh"] == 0.5  # unknown / not applicable questions are left out
    assert axes({"id": 2, "config_json": {"k": 5}}, [])["fresh"] is None


def test_extra_axes_come_from_the_axis_source_and_turn_lower_around():
    """A registered suite's 0-1 metrics become axes after the canvas ones (d3, c35)."""
    from culture_library.core import eval_radar as er

    run = {"id": 1, "config_json": {"k": 5}, "aggregates_json": {"x_good": 0.7, "x_bad": 0.2}}
    previous = er._axis_source
    try:
        er.set_axis_source(lambda: [("x_good", "higher"), ("x_bad", "lower"), ("mrr", "higher")])
        ax = er.axes(run, [])
        assert er.axis_names()[: len(er.AXES)] == er.AXES
        assert er.axis_names()[len(er.AXES) :] == ("x_good", "x_bad")  # mapped names skipped
        assert ax["x_good"] == 0.7
        assert abs(ax["x_bad"] - 0.8) < 1e-9
        assert set(er.group([(run, [])])["axes"]) == set(er.axis_names())
        er.set_axis_source(None)
        assert er.axis_names() == er.AXES
    finally:
        er.set_axis_source(previous)


def test_failed_questions_count_on_no_errors_only():
    """A failed retrieval is not also scored as a miss on the ID axes."""
    run = _run(error_rate=0.25)
    samples = [_s(i, ["a"], ["a"]) for i in range(1, 4)]
    samples.append(_s(4, [], ["a"], scores={"error_rate": 1.0}))
    out = er.axes(run, samples)
    assert out["found_at_1"] == 1.0
    assert out["no_errors"] == pytest.approx(0.75)


def test_compare_changed_questions_are_item_aware():
    """changed_questions scores the same way as the axes: a shared sha in another item misses."""
    ids_other = {"retrieved_item_ids": [70]}
    ids_own = {"retrieved_item_ids": [68]}
    before = [_s(1, ["shared"], ["shared"], q="one", **ids_other)]
    after = [_s(1, ["shared"], ["shared"], q="one", **ids_own)]
    out = er.compare((_run(1, item_id=68), before), (_run(2, item_id=68), after))
    assert out["deltas"]["found_at_1"] == 1.0
    [changed] = out["changed_questions"]
    assert changed["before"]["found_at_1"] == 0.0
    assert changed["after"]["found_at_1"] == 1.0


def test_failed_questions_known_from_config_errors_without_speed_suite():
    """config.errors names the failed samples even when the speed suite did not run."""
    run = _run(error_rate=0.5, config_json={"k": 5, "errors": [{"sample_id": 2, "error": "x"}]})
    samples = [_s(1, ["a"], ["a"]), _s(2, [], ["a"])]
    assert er.axes(run, samples)["found_at_1"] == 1.0


def test_breakdown_slice_whose_questions_all_failed_stays_weakest():
    """A slice has no no_errors axis, so its failures still score as misses and sort first."""
    run = _run(error_rate=0.5, config_json={"k": 5, "errors": [{"sample_id": 2, "error": "x"}]})
    samples = [_s(1, ["a"], ["a"]), _s(2, [], ["a"])]
    sources = [{"id": 1, "source_urls": ["http://ok"]}, {"id": 2, "source_urls": ["http://bad"]}]
    out = er.breakdown(run, samples, sources)
    assert out["sources"][0]["url"] == "http://bad"
    assert out["sources"][0]["axes"]["found_at_5"] == 0.0


def test_compare_leaves_out_questions_that_failed_on_either_side():
    before = [_s(1, ["a"], ["a"], q="one")]
    after = [_s(1, [], ["a"], q="one")]
    run_b = _run(
        2,
        error_rate=1.0,
        config_json={"k": 5, "judge_model": "cortex", "errors": [{"sample_id": 1}]},
    )
    out = er.compare((_run(1), before), (run_b, after))
    assert out["changed_questions"] == []


def test_result_without_reference_snapshot_keeps_its_stored_id_scores():
    """A backfill withdrew the references (set re-anchored since): use what the run stored."""
    row = {
        "sample_id": 1,
        "question": "q",
        "retrieved_chunk_shas": ["a", "b"],
        "reference_chunk_shas": None,
        "scores_json": {"found_at_1": 0.0, "found_at_5": 1.0, "mrr": 0.5},
    }
    got = er._question_metrics(row, 5)
    assert (got["found_at_1"], got["found_at_5"], got["mrr"]) == (0.0, 1.0, 0.5)
    assert got["ndcg"] is None  # never stored, so not measured


def test_result_with_empty_references_is_still_rescored():
    row = {
        "sample_id": 1,
        "question": "q",
        "retrieved_chunk_shas": ["a"],
        "reference_chunk_shas": [],
        "scores_json": {"found_at_1": 1.0},
    }
    assert er._question_metrics(row, 5)["found_at_1"] is None
