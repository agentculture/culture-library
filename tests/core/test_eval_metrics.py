"""ID retrieval metrics from stored ranks (pure arithmetic, no DB, no network)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from culture_library.core.eval_metrics import id_metrics

FIXTURE = Path(__file__).parent / "fixtures" / "eval_runs_11_19.json"


def _run_means(run_id: str) -> dict:
    samples = json.loads(FIXTURE.read_text())[run_id]
    rows = [id_metrics(s["retrieved_chunk_shas"], s["reference_chunk_shas"], 5) for s in samples]
    assert len(rows) == 20
    assert all(r["sha_only"] is True for r in rows)
    return {
        "found_at_1": sum(r["found_at_1"] for r in rows) / len(rows),
        "mrr": sum(r["mrr"] for r in rows) / len(rows),
    }


def test_run_19_rerank_off_from_stored_ranks():
    m = _run_means("19")
    assert m["found_at_1"] == pytest.approx(0.20)
    assert round(m["mrr"], 3) == 0.435


def test_run_11_rerank_on_from_stored_ranks():
    m = _run_means("11")
    assert m["found_at_1"] == pytest.approx(0.45)
    assert round(m["mrr"], 3) == 0.618


def test_depth_beyond_k_run_is_none_not_zero():
    r = id_metrics(["a", "b", "c", "d", "e"], ["z"], 5)
    assert r["found_at_10"] is None
    assert r["found_at_5"] == 0.0
    assert r["found_at_1"] == 0.0
    r10 = id_metrics(["a"] * 10, ["a"], 10)
    assert r10["found_at_10"] == 1.0


def test_found_at_depths():
    r = id_metrics(["a", "b", "c", "d", "e"], ["c"], 5)
    assert (r["found_at_1"], r["found_at_3"], r["found_at_5"]) == (0.0, 1.0, 1.0)
    assert r["mrr"] == pytest.approx(1 / 3)
    assert r["ndcg"] == pytest.approx(1 / math.log2(4))
    assert r["k"] == 5
    assert r["depth"] == 5  # d2: a k=5 run is measured at depth 5, not labelled @10


def test_precision_at_k_ceiling_is_one_over_k():
    r = id_metrics(["a", "b", "c", "d", "e"], ["a"], 5)
    assert r["precision_at_k"] == pytest.approx(0.2)
    two = id_metrics(["a", "b", "c", "d", "e"], ["a", "b"], 5)
    assert two["precision_at_k"] == pytest.approx(0.4)


def test_duplicate_retrieved_passage_counts_once():
    r = id_metrics(["a", "a", "a", "b", "c"], ["a"], 5)
    assert r["precision_at_k"] == pytest.approx(0.2)


def test_no_hit_is_zero_and_empty_references_are_none():
    r = id_metrics(["a", "b"], ["z"], 5)
    assert r["mrr"] == 0.0
    assert r["ndcg"] == 0.0
    assert r["precision_at_k"] == 0.0
    empty = id_metrics(["a"], [], 5)
    assert empty["found_at_1"] is None
    assert empty["mrr"] is None


def test_ndcg_binary_multiple_references_perfect_is_one():
    r = id_metrics(["a", "b", "x"], ["a", "b"], 5)
    assert r["ndcg"] == pytest.approx(1.0)
    swapped = id_metrics(["x", "a", "b"], ["a", "b"], 5)
    ideal = 1 + 1 / math.log2(3)
    assert swapped["ndcg"] == pytest.approx((1 / math.log2(3) + 1 / 2) / ideal)


def test_item_aware_same_sha_in_other_item_is_not_found():
    retrieved = [(2, "shared"), (3, "other")]
    refs = [(1, "shared")]
    r = id_metrics(retrieved, refs, 5)
    assert r["found_at_5"] == 0.0
    assert r["mrr"] == 0.0
    assert r["sha_only"] is False


def test_item_aware_match_requires_both():
    r = id_metrics([(2, "x"), (1, "shared")], [(1, "shared")], 5)
    assert r["found_at_1"] == 0.0
    assert r["found_at_3"] == 1.0
    assert r["mrr"] == pytest.approx(0.5)
    assert r["sha_only"] is False


def test_sha_only_when_either_side_lacks_item_ids():
    r = id_metrics([(2, "shared")], ["shared"], 5)
    assert r["found_at_1"] == 1.0
    assert r["sha_only"] is True
    r = id_metrics(["shared"], [(1, "shared")], 5)
    assert r["found_at_1"] == 1.0
    assert r["sha_only"] is True


def test_ndcg_short_result_list_is_not_perfect():
    """The ideal fills min(10, k) ranks, so one relevant passage of three is not nDCG 1."""
    r = id_metrics(["a"], ["a", "b", "c"], 5)
    ideal = 1 + 1 / math.log2(3) + 1 / 2
    assert r["ndcg"] == pytest.approx(1 / ideal)
