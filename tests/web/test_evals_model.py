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
    assert rows[0]["off"] == {"run": 8, "v": 0.75}
    assert rows[0]["on"] == {"run": 5, "v": 1.0}
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
    assert none["on"] is None
    assert none["sets"] == 0
    judge = _model("m.judgeTile(picked, axes)")
    assert judge["runs"] == 2
    assert abs(judge["value"] - 0.7) < 1e-9
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


# ---------- radar (plan task t17) ----------


@pytest.mark.parametrize(
    "params,want",
    [
        ("", {"runs": [], "vs": None}),
        ("runs=11", {"runs": [11], "vs": None}),
        ("runs=11,18,20&vs=19", {"runs": [11, 18, 20], "vs": [19]}),
        ("runs=3,3,x,-1,0,4&vs=none", {"runs": [3, 4], "vs": "none"}),
        ("vs=2,5&runs=1", {"runs": [1], "vs": [2, 5]}),
        ("runs=1&vs=", {"runs": [1], "vs": None}),
    ],
)
def test_radar_params(params: str, want: dict) -> None:
    assert _js("hashroute.js", f"m.radarParams({json.dumps(params)})") == want


def test_radar_hash_round_trips_through_parse_hash() -> None:
    got = _js(
        "hashroute.js",
        "[m.radarHash({runs: [11, 18], vs: [19]}), m.radarHash({runs: [4], vs: 'none'}),"
        " m.radarHash({runs: [4]}), m.radarHash({})]",
    )
    assert got == [
        "#evals/radar?runs=11,18&vs=19",
        "#evals/radar?runs=4&vs=none",
        "#evals/radar?runs=4",
        "#evals/radar",
    ]
    back = _js(
        "hashroute.js",
        "m.radarParams(m.parseHash(m.radarHash({runs: [7, 8], vs: [2]}), () => false).params)",
    )
    assert back == {"runs": [7, 8], "vs": [2]}


def _suites(*names: str) -> list[dict]:
    return [{"name": n, "version": "1"} for n in names]


RADAR_RUNS = [
    # set 1 (book): an id-only pair, then a judged rerank-on run
    _run(1, 1, "complete", False, 20, suites=_suites("id_retrieval")),
    _run(2, 1, "complete", True, 20, suites=_suites("id_retrieval")),
    _run(
        9,
        1,
        "complete",
        True,
        20,
        suites=_suites("id_retrieval", "judged"),
        config={"judge_model": "cortex"},
    ),
    # set 2 (book): rerank on twice with the same suites, and a later failed run
    _run(3, 2, "complete", True, 20, suites=_suites("id_retrieval")),
    _run(10, 2, "complete", True, 20, suites=_suites("id_retrieval")),
    _run(12, 2, "failed", True, 20, suites=_suites("id_retrieval")),
    # set 3 (newspaper): rerank on only
    _run(4, 3, "complete", True, 4, suites=_suites("id_retrieval")),
    _run(11, 3, "complete", True, 4, suites=_suites("id_retrieval")),
]
RADAR_SETS = [{"id": 1, "item_id": 10}, {"id": 2, "item_id": 20}, {"id": 3, "item_id": 30}]
RADAR_SETUP = (
    f"const runs = {json.dumps(RADAR_RUNS)}, sets = {json.dumps(RADAR_SETS)};"
    "const info = id => ({10: {title: 'Book A', kind: 'book'}, 20: {title: 'Book B', kind: 'book'},"
    " 30: {title: 'Paper C', kind: 'newspaper'}})[id];"
    "const byId = ids => ids.map(i => runs.find(r => r.id === i));"
)


def _radar(expr: str):
    uri = json.dumps((STATIC / "evals-model.js").as_uri())
    script = f"import({uri}).then(m => {{ {RADAR_SETUP} console.log(JSON.stringify({expr})); }})"
    out = subprocess.run(  # nosec B603 - fixed argv
        [NODE, "--input-type=module", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_config_key_and_text() -> None:
    assert _radar("m.configKey(runs[0])") == "hybrid|5|off"
    assert _radar("m.configText(m.configKey(runs[1]))") == "hybrid, k 5, rerank on"
    assert _radar("m.configKey({config: {}})") == "?|?|off"


def test_set_groups_for_the_documents_picker() -> None:
    got = _radar("m.setGroups(sets, runs, info)")
    assert got == [
        {"id": "book", "name": "All books", "label": "All books, sets 1, 2", "sets": [1, 2]},
        {
            "id": "newspaper",
            "name": "All newspapers",
            "label": "All newspapers, set 3",
            "sets": [3],
        },
        {"id": "all", "name": "Everything", "label": "Everything, sets 1–3", "sets": [1, 2, 3]},
    ]
    assert _radar("m.setsText([1, 2, 3, 5])") == "sets 1–3, 5"
    assert _radar("m.setsText([4])") == "set 4"


def test_configs_for_sets_most_sets_first_rerank_on_first() -> None:
    got = _radar("m.configsFor(runs, [1, 2, 3])")
    assert got == [{"key": "hybrid|5|on", "sets": 3}, {"key": "hybrid|5|off", "sets": 1}]


def test_pick_runs_latest_complete_per_set_with_one_config() -> None:
    got = _radar("m.pickRuns(runs, [1, 2, 3], 'hybrid|5|on')")
    assert [r["id"] for r in got["runs"]] == [9, 10, 11]
    assert got["missing"] == []
    off = _radar("m.pickRuns(runs, [1, 2], 'hybrid|5|off')")
    assert [r["id"] for r in off["runs"]] == [1]
    assert off["missing"] == [2]


def test_earlier_comparable_runs_need_every_set() -> None:
    # Set 1's latest rerank-on run (9) is judged; the earlier rerank-on run (2) is not, so
    # there is no earlier comparable run for set 1 and the group has no overlay.
    got = _radar("m.earlierRuns(runs, byId([9, 10, 11]))")
    assert [r["id"] for r in got["runs"]] == [3, 4]
    assert got["missing"] == [1]
    ok = _radar("m.earlierRuns(runs, byId([10, 11]))")
    assert [r["id"] for r in ok["runs"]] == [3, 4]
    assert ok["missing"] == []
    assert _radar("m.earlierRuns(runs, byId([1])).missing") == [1]


def test_selection_of_runs_maps_back_to_the_pickers() -> None:
    groups = "m.setGroups(sets, runs, info)"
    assert _radar(f"m.selectionOf(byId([9, 10]), {groups})") == {
        "scope": "set",
        "docs": "book",
        "key": "hybrid|5|on",
    }
    assert _radar(f"m.selectionOf(byId([4]), {groups})") == {
        "scope": "one",
        "docs": "set:3",
        "key": "hybrid|5|on",
    }
    assert _radar(f"m.selectionOf(byId([1, 4]), {groups})") == {
        "scope": "set",
        "docs": "custom",
        "key": "mixed",
    }


def test_default_view_prefers_books_and_the_config_most_sets_ran() -> None:
    assert _radar("m.defaultView(runs, m.setGroups(sets, runs, info))") == {
        "docs": "book",
        "sets": [1, 2],
        "key": "hybrid|5|on",
    }
    assert _radar("m.defaultView([], [])") is None


# ---------- compare (plan task t18) ----------


@pytest.mark.parametrize(
    "params,want",
    [
        ("", {"before": [], "after": []}),
        ("before=19&after=11", {"before": [19], "after": [11]}),
        ("after=3,4&before=1,2,2,x", {"before": [1, 2], "after": [3, 4]}),
        ("before=5", {"before": [5], "after": []}),
    ],
)
def test_compare_params(params: str, want: dict) -> None:
    assert _js("hashroute.js", f"m.compareParams({json.dumps(params)})") == want


def test_compare_hash_round_trips_through_parse_hash() -> None:
    got = _js(
        "hashroute.js",
        "[m.compareHash({before: [19], after: [11]}),"
        " m.compareHash({before: [1, 2], after: [3, 4]}), m.compareHash({})]",
    )
    assert got == [
        "#evals/compare?before=19&after=11",
        "#evals/compare?before=1,2&after=3,4",
        "#evals/compare",
    ]
    back = _js(
        "hashroute.js",
        "m.compareParams(m.parseHash("
        "m.compareHash({before: [7], after: [8, 9]}), () => false).params)",
    )
    assert back == {"before": [7], "after": [8, 9]}


def test_compare_question_names_what_differs() -> None:
    q = _radar("m.compareQuestion(byId([1]), byId([2]))")
    assert q["question"] == "Did turning rerank on help?"
    assert q["settings"] == "Only one setting differs: rerank off, then on"
    assert _radar("m.compareQuestion(byId([2]), byId([1])).question") == (
        "Did turning rerank off help?"
    )
    same = _radar("m.compareQuestion(byId([3]), byId([10]))")
    assert same["question"] == "What changed since run 3?"
    assert same["settings"] == "No setting differs: hybrid, k 5, rerank on"
    assert _radar("m.compareQuestion(byId([3, 4]), byId([10, 11])).question") == (
        "What changed since runs 3, 4?"
    )
    two = _radar(
        "m.compareQuestion([{id: 1, set_id: 1, config: {mode: 'semantic', k: 5}}],"
        " [{id: 2, set_id: 1, config: {mode: 'hybrid', k: 10}}])"
    )
    assert two["question"] == (
        "Did switching from semantic to hybrid search and changing k from 5 to 10 help?"
    )
    assert two["settings"] == "2 settings differ: semantic, then hybrid search; k 5, then 10"
    mixed = _radar("m.compareQuestion(byId([1, 3]), byId([2, 10]))")
    assert mixed["question"] == "How do these runs compare?"
    assert mixed["settings"] == "Mixed search settings on one side"
    other_set = _radar("m.compareQuestion(byId([3]), byId([11])).question")
    assert other_set == "How do these runs compare?"


OK_CHECKS = {"comparable": True, "reasons": [], "warnings": []}
NO_LEFT = {"before": 0, "after": 0}


def test_check_rows_list_what_holds_then_warnings() -> None:
    rows = _radar(
        f"m.checkRows(byId([1]), byId([2]), {json.dumps(OK_CHECKS)}, {json.dumps(NO_LEFT)})"
    )
    assert rows == [
        {"state": "ok", "text": "Same question set", "detail": "Set 1, 20 questions"},
        {"state": "ok", "text": "Same suites and judge", "detail": "ID retrieval; no judge"},
        {
            "state": "ok",
            "text": "Both complete",
            "detail": "Only one setting differs: rerank off, then on",
        },
    ]
    warned = {"comparable": True, "reasons": [], "warnings": ["set 1: suite version differs: a"]}
    v = _radar(
        "m.checkRows([{...byId([9])[0], set_version: 2, error_rate: 0}],"
        " [{...byId([9])[0], id: 13, set_version: 2, error_rate: 0.05}],"
        f" {json.dumps(warned)}, {{before: 0, after: 2}})"
    )
    assert v[0] == {
        "state": "ok",
        "text": "Same question set and version",
        "detail": "Set 1, version 2, 20 questions",
    }
    assert v[1]["detail"] == "ID retrieval and judged retrieval; judge cortex"
    assert v[2] == {
        "state": "ok",
        "text": "Both complete, 1 error",
        "detail": "No setting differs: hybrid, k 5, rerank on",
    }
    assert v[3:] == [
        {"state": "warn", "text": "Set 1: suite version differs: a", "detail": None},
        {
            "state": "warn",
            "text": "2 runs left out on the after side",
            "detail": "This key cannot see their items.",
        },
    ]


def test_check_rows_put_reasons_first_and_drop_what_fails() -> None:
    bad = {"comparable": False, "reasons": ["different suites: a vs b"], "warnings": []}
    rows = _radar(f"m.checkRows(byId([1]), byId([9]), {json.dumps(bad)}, {json.dumps(NO_LEFT)})")
    assert rows[0] == {"state": "fail", "text": "Different suites: a vs b", "detail": None}
    assert [r["text"] for r in rows[1:]] == ["Same question set", "Both complete"]
    group = _radar(
        f"m.checkRows(byId([3, 4]), byId([10, 11]), {json.dumps(OK_CHECKS)}, {json.dumps(NO_LEFT)})"
    )
    assert group[0] == {
        "state": "ok",
        "text": "Same question sets",
        "detail": "Sets 2, 3, 24 questions",
    }
    assert group[2]["text"] == "All complete"


def test_tally_and_order_by_change() -> None:
    setup = "const k = ['a', 'b', 'c', 'd', 'e'], d = {a: 0.2, b: -0.1, c: 0.001, d: null, e: 0.3};"
    got = _radar(f"(() => {{ {setup} return [m.tally(k, d), m.byChange(k, d)]; }})()")
    assert got[0] == {"better": ["a", "e"], "worse": ["b"], "same": ["c"], "unscored": ["d"]}
    assert got[1] == ["e", "a", "c", "b", "d"]


CHANGED = [
    {
        "question": "Q worse",
        "before": {"found_at_1": 1, "found_at_3": 1, "found_at_5": 1, "mrr": 1},
        "after": {"found_at_1": 0, "found_at_3": 1, "found_at_5": 1, "mrr": 0.5},
        "delta": -0.375,
    },
    {
        "question": "Q mixed",
        "before": {"found_at_1": 0, "found_at_3": 1, "found_at_5": 1, "mrr": 0.5},
        "after": {"found_at_1": 1, "found_at_3": 0, "found_at_5": 0, "mrr": 1},
        "delta": -0.125,
    },
    {
        "question": "Q better",
        "before": {"found_at_1": 0, "found_at_3": 0, "found_at_5": 0, "mrr": 0},
        "after": {"found_at_1": 0, "found_at_3": 1, "found_at_5": 1, "mrr": 0.3333},
        "delta": 0.58,
    },
]


def test_question_groups_found_shift_and_rank() -> None:
    c = json.dumps(CHANGED)
    groups = _radar(f"m.questionGroups({c})")
    assert [q["question"] for q in groups["worse"]] == ["Q worse", "Q mixed"]
    assert [q["question"] for q in groups["better"]] == ["Q better"]
    assert _radar(f"m.foundShift({c})") == {"gained": 1, "lost": 1}
    assert _radar("[1, 0.5, 0.3333, 0, null].map(m.rankOf)") == [1, 2, 3, None, None]


def test_compare_options_for_the_pickers() -> None:
    opts = _radar("m.compareOptions(runs, m.setGroups(sets, runs, info), info, sets)")
    groups = [o for o in opts if o["value"].startswith("g:")]
    assert groups[:2] == [
        {
            "value": "g:book:hybrid|5|on",
            "label": "All books, hybrid, k 5, rerank on",
            "ids": [9, 10],
            "group": "Sets of documents",
        },
        {
            "value": "g:book:hybrid|5|off",
            "label": "All books, hybrid, k 5, rerank off (1 of 2 sets)",
            "ids": [1],
            "group": "Sets of documents",
        },
    ]
    runs_ = [o for o in opts if o["value"].startswith("r:")]
    assert [o["ids"] for o in runs_] == [
        [11],
        [10],
        [9],
        [4],
        [3],
        [2],
        [1],
    ]  # complete, newest first
    assert runs_[0] == {
        "value": "r:11",
        "label": "Run 11, Paper C, hybrid, k 5, rerank on",
        "ids": [11],
        "group": "One run",
    }
    assert _radar(
        "m.optionFor([10, 9], m.compareOptions(runs, m.setGroups(sets, runs, info), info, sets))"
    ) == ("g:book:hybrid|5|on")
    assert (
        _radar(
            "m.optionFor([1, 4], m.compareOptions(runs, m.setGroups(sets, runs, info), info, sets))"
        )
        is None
    )


def test_default_compare_prefers_a_rerank_pair_of_the_book_sets() -> None:
    assert _radar("m.defaultCompare(runs, m.setGroups(sets, runs, info))") == {
        "before": [1],
        "after": [2],
        "why": "rerank",
        "group": "book",
    }
    # Without any rerank-off run: the newest complete run against its earlier comparable run.
    no_off = "runs.filter(r => r.config.rerank)"
    assert _radar(f"m.defaultCompare({no_off}, m.setGroups(sets, {no_off}, info))") == {
        "before": [4],
        "after": [11],
        "why": "earlier",
        "group": None,
    }
    assert _radar("m.defaultCompare([], [])") is None


def test_rank_depth_is_min_10_k_per_run_or_mixed() -> None:
    """Deviation d4: the depth MRR/nDCG were measured at, from the runs' recorded k."""

    def depth(ks: list) -> object:
        runs = json.dumps([{"id": i, "config": {"k": k}} for i, k in enumerate(ks)])
        return _js("evals-model.js", f"m.rankDepth({runs})")

    assert depth([5, 5]) == 5
    assert depth([20]) == 10
    assert depth([5, 10]) == "mixed"
    assert depth([None]) is None
    assert _js("evals-model.js", "m.rankDepth([])") is None
