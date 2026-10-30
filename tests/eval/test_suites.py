"""The suite registry, id_retrieval and judged suites."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from culture_library.core.eval_metrics import id_metrics
from culture_library.eval import suites
from culture_library.eval.config import JudgeConfig
from culture_library.eval.models import Passage, Sample, ScoreResult
from culture_library.eval.suites.base import HIGHER, Metric, SuiteContext, SuiteScore
from culture_library.eval.suites.judged import JudgedSuite

from .conftest import FakeJudge

ROOT = Path(__file__).resolve().parents[2]


def _passages(*shas, item_id=1, text="t"):
    return [Passage(item_id=item_id, chunk_sha=s, text=text) for s in shas]


def test_builtin_suites_registered_and_catalog_is_json():
    assert {"id_retrieval", "judged"} <= set(suites.names())
    cat = json.loads(json.dumps(suites.catalog()))
    by = {s["name"]: s for s in cat}
    assert by["judged"]["needs_judge"] is True
    assert by["id_retrieval"]["needs_judge"] is False
    for s in cat:
        assert s["version"]
        for m in s["metrics"]:
            assert set(m) == {"name", "definition", "range", "direction"}
            assert m["direction"] in ("higher", "lower")
    mrr = next(m for m in by["id_retrieval"]["metrics"] if m["name"] == "mrr")
    assert "min(10, k)" in mrr["definition"]


def test_every_metric_belongs_to_one_suite():
    keys = [m.name for s in suites.all_suites() for m in s.metrics]
    assert len(keys) == len(set(keys))


def test_id_retrieval_matches_core_metrics():
    s = Sample(question="q", reference_chunk_shas=["b"])
    ctx = SuiteContext([s], [_passages("a", "b", "c")], k=5)
    out = suites.get("id_retrieval").score(ctx)
    want = id_metrics(["a", "b", "c"], ["b"], 5)
    assert out.skipped is None
    assert out.per_sample[0]["mrr"] == want["mrr"] == 0.5
    assert out.per_sample[0]["found_at_10"] is None
    assert out.aggregate["found_at_3"] == 1.0
    assert set(out.per_sample[0]) == {m.name for m in suites.get("id_retrieval").metrics}


class _Adapter:
    def __init__(self):
        self.seen = None

    def score(self, samples, retrieved, llm):
        self.seen = retrieved
        return ScoreResult(
            per_sample=[{"context_precision": 1.0, "context_recall": 0.5, "x": 9}] * len(samples),
            aggregate={"context_precision": 1.0, "context_recall": 0.5},
        )


def _judged(adapter):
    return JudgedSuite(adapter_factory=lambda judge: adapter)


def _ctx(vis, judge, is_public=None):
    return SuiteContext(
        [Sample(question="q", reference_answer="a", reference_chunk_shas=["a"])],
        [[*_passages("a", item_id=1, text="own"), *_passages("z", item_id=2, text="other")]],
        k=5,
        item={"id": 1, "visibility": vis},
        judge=judge,
        llm=FakeJudge(),
        is_public=is_public,
    )


def test_judged_skips_private_item_with_remote_judge():
    ad = _Adapter()
    out = _judged(ad).score(_ctx("private", JudgeConfig(url="https://judge.example.com")))
    assert out.skipped
    assert "item 1 is not public" in out.skipped
    assert ad.seen is None
    assert out.per_sample == []


def test_judged_local_judge_scores_private_item_unblanked():
    ad = _Adapter()
    out = _judged(ad).score(_ctx("private", JudgeConfig(url="http://127.0.0.1:1")))
    assert out.skipped is None
    assert out.aggregate == {"context_precision": 1.0, "context_recall": 0.5}
    assert out.per_sample[0] == {"context_precision": 1.0, "context_recall": 0.5}
    assert [p.text for p in ad.seen[0]] == ["own", "other"]


def test_judged_remote_judge_blanks_nonpublic_other_items():
    ad = _Adapter()
    judge = JudgeConfig(url="https://judge.example.com")
    _judged(ad).score(_ctx("public", judge, is_public=lambda i: False))
    assert [p.text for p in ad.seen[0]] == ["own", ""]


def test_judged_without_judge_is_skipped():
    out = _judged(_Adapter()).score(SuiteContext([], [], k=5))
    assert out.skipped


class _Fake:
    name = "fake"
    version = "1"
    needs_judge = False
    metrics = (Metric("fake_metric", "d", (0.0, 1.0), HIGHER),)

    def score(self, ctx):
        return SuiteScore([], {"fake_metric": 1.0})


def test_registered_context_manager_registers_and_undoes():
    with suites.registered(_Fake()):
        assert "fake" in suites.names()
        assert suites.catalog()[[s["name"] for s in suites.catalog()].index("fake")]
    assert "fake" not in suites.names()


def test_duplicate_suite_or_metric_rejected():
    with suites.registered(_Fake()):
        duplicate = _Fake()
        with pytest.raises(ValueError):
            suites.register(duplicate)

    class Clash(_Fake):
        name = "clash"
        metrics = (Metric("mrr", "d", (0.0, 1.0), HIGHER),)

    clash = Clash()
    with pytest.raises(ValueError):
        suites.register(clash)
    assert "clash" not in suites.names()


def test_get_unknown_suite():
    with pytest.raises(KeyError):
        suites.get("nope")


def test_bad_direction_rejected():
    with pytest.raises(ValueError):
        Metric("m", "d", (0, 1), "sideways")


_NO_EXTRA = """
import sys

class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in {'ragas', 'openai', 'fastapi', 'httpx', 'langchain'}:
            raise ImportError('blocked: ' + name)

sys.meta_path.insert(0, Block())
from culture_library.eval import suites
assert {'id_retrieval', 'judged'} <= set(suites.names())
assert 'ragas' not in sys.modules
"""


def test_suites_import_without_eval_extra():
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _NO_EXTRA],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT)},
    )
    assert proc.returncode == 0, proc.stderr
