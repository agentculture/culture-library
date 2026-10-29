"""The Evals tab's pure modules under Node (plan task t16): ``hashroute.js`` routing and
``evals-model.js`` (latest complete run per set and rerank setting, dumbbell rows, tiles).

Skipped when Node is not installed; the modules have no DOM and no imports.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 - fixed argv
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "culture_library" / "web" / "static"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _js(module: str, body: str):
    """Run ``body`` (an expression over ``m``, the imported module) and return its JSON."""
    uri = json.dumps((STATIC / module).as_uri())
    script = f"import({uri}).then(m => console.log(JSON.stringify({body})))"
    out = subprocess.run(  # nosec B603 - fixed argv
        [NODE, "--input-type=module", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


KEYS = "k => ['item-1', 'doc-2', 'item-12'].includes(k)"


@pytest.mark.parametrize(
    "hash_,want",
    [
        ("#evals", {"view": "evals", "page": "overview", "params": ""}),
        ("evals", {"view": "evals", "page": "overview", "params": ""}),
        ("#evals/radar", {"view": "evals", "page": "radar", "params": ""}),
        ("#evals/compare", {"view": "evals", "page": "compare", "params": ""}),
        (
            "#evals/compare?before=1&after=2",
            {"view": "evals", "page": "compare", "params": "before=1&after=2"},
        ),
        ("#evals/nope", {"view": "evals", "page": "overview", "params": ""}),
        ("#evals?x=1", {"view": "evals", "page": "overview", "params": "x=1"}),
        ("#state", {"view": "state"}),
        ("#item-1", {"view": "read", "key": "item-1"}),
        ("#doc-2", {"view": "read", "key": "doc-2"}),
        ("#item-99", {"view": "home"}),
        ("#home", {"view": "home"}),
        ("", {"view": "home"}),
        ("#evalsx", {"view": "home"}),
    ],
)
def test_parse_hash(hash_: str, want: dict) -> None:
    assert _js("hashroute.js", f"m.parseHash({json.dumps(hash_)}, {KEYS})") == want


def test_evals_route_wins_even_if_an_item_key_matched() -> None:
    # Item keys are item-N / doc-N, but the evals segment is checked first regardless.
    assert _js("hashroute.js", "m.parseHash('#evals/radar', () => true)")["view"] == "evals"
    assert _js("hashroute.js", "m.parseHash('#state', () => true)") == {"view": "state"}


def test_evals_hash() -> None:
    got = _js("hashroute.js", "['overview', 'radar', 'compare', ''].map(m.evalsHash)")
    assert got == ["#evals", "#evals/radar", "#evals/compare", "#evals"]


def _run(id_: int, set_id: int, status: str, rerank, total: int, **extra) -> dict:
    config = {"mode": "hybrid", "k": 5}
    if rerank is not None:
        config["rerank"] = rerank
    config.update(extra.pop("config", {}))
    return {
        "id": id_,
        "set_id": set_id,
        "status": status,
        "config": config,
        "total": total,
        **extra,
    }


RUNS = [
    _run(1, 1, "complete", False, 20),
    _run(2, 1, "partial", False, 20, error_rate=0.1),
    _run(3, 1, "complete", True, 20),
    _run(4, 1, "complete", False, 20),
    _run(5, 2, "complete", True, 4, config={"judge_model": "cortex"}),
    _run(6, 2, "failed", False, 4, error_rate=1.0),
    {"id": 7, "set_id": 3, "status": "running", "config": {"rerank": True}, "total": 4},
    _run(8, 2, "complete", None, 4),  # no recorded rerank: counts as off
]
SETS = [{"id": 1, "item_id": 10}, {"id": 2, "item_id": 20}, {"id": 3, "item_id": 30}]
AXES = {
    1: {"found_at_5": 0.5},
    3: {"found_at_5": 0.9, "judge_precision": 0.8},
    4: {"found_at_5": 0.7},
    5: {"found_at_5": 1.0, "judge_precision": 0.6},
    8: {"found_at_5": 0.75},
}
SETUP = (
    f"const runs = {json.dumps(RUNS)}, sets = {json.dumps(SETS)};"
    f"const axes = new Map(Object.entries({json.dumps(AXES)}).map(([k, v]) => [Number(k), v]));"
    "const questions = new Map(runs.map(r => [r.id, r.total]));"
    "const info = id => ({10: {title: 'Book A', kind: 'book'},"
    " 20: {title: 'Paper B', kind: 'newspaper'}})[id];"
    "const picked = m.latestComplete(runs);"
)


def _model(expr: str):
    uri = json.dumps((STATIC / "evals-model.js").as_uri())
    script = f"import({uri}).then(m => {{ {SETUP} console.log(JSON.stringify({expr})); }})"
    out = subprocess.run(  # nosec B603 - fixed argv
        [NODE, "--input-type=module", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_latest_complete_run_per_set_and_rerank_setting() -> None:
    got = _model("[...picked].map(([s, p]) => [s, p.off && p.off.id, p.on && p.on.id])")
    # set 1: off -> 4 (newer than 1; partial 2 never counts), on -> 3
    # set 2: on -> 5, off -> 8 (missing rerank means off; failed 6 never counts)
    # set 3: only a running run, so no entry
    assert got == [[1, 4, 3], [2, 8, 5]]


def test_rerank_rows_sorted_by_gain_with_titles() -> None:
    rows = _model("m.rerankRows(sets, picked, axes, questions, info)")
    assert [(r["set_id"], r["title"], r["kind"]) for r in rows] == [
        (2, "Paper B", "newspaper"),
        (1, "Book A", "book"),
    ]
    assert rows[0]["off"] == {"run": 8, "v": 0.75} and rows[0]["on"] == {"run": 5, "v": 1.0}
    assert rows[1]["questions"] == 20
    assert abs(rows[1]["gain"] - 0.2) < 1e-9


def test_rows_with_one_side_sort_last_and_unknown_items_are_named() -> None:
    expr = (
        "m.rerankRows([...sets, {id: 9, item_id: 90}],"
        " new Map([...picked, [9, {off: null, on: {id: 3}}]]), axes, questions, info)"
        ".map(r => [r.set_id, r.title, r.gain])"
    )
    got = _model(expr)
    assert got[-1] == [9, "Item 90", None]


def test_domain_and_headline_and_judge_and_counts() -> None:
    assert _model("m.domain([0.5, 0.75, 1])") == [0.4, 1]
    assert _model("m.domain([0.95])") == [0.8, 1]
    assert _model("m.domain([0.0])") == [0, 1]
    assert _model("m.domain([0.4])") == [0.4, 1]
    assert _model("m.domain([])") == [0, 1]
    book = _model("m.headline(m.rerankRows(sets, picked, axes, questions, info), 'book')")
    assert book == {
        "sets": 1,
        "questions": 20,
        "on": 0.9,
        "off": 0.7,
        "onSets": 1,
        "offSets": 1,
    }
    none = _model("m.headline([], 'book')")
    assert none["on"] is None and none["sets"] == 0
    judge = _model("m.judgeTile(picked, axes)")
    assert judge["runs"] == 2 and abs(judge["value"] - 0.7) < 1e-9
    assert judge["judges"] == ["cortex"]
    counts = _model("m.runCounts(runs)")
    assert counts == {
        "total": 8,
        "complete": 5,
        "partial": 1,
        "failed": 1,
        "running": 1,
        "errors": 6,
        "questions": 96,
    }


def test_labels_missed_and_order() -> None:
    assert _model("m.searchLabel([runs[0], runs[2]])") == "hybrid, k 5"
    assert _model("m.searchLabel([runs[0], runs[6]])") == "mixed search settings"
    assert _model("m.runSearch(runs[2])") == "hybrid, k 5, rerank on"
    assert _model("m.runSearch(runs[6])") == "mode ?, k ?, rerank on"
    assert _model("[m.missed(20, 0.75), m.missed(4, 1), m.missed(4, null)]") == [5, 0, None]
    assert _model("m.newestFirst(runs).map(r => r.id)") == [8, 7, 6, 5, 4, 3, 2, 1]
