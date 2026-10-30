"""The shape chart's geometry under Node (plan task t17): ``evals-geom.js``.

The design canvas "Radar" board fixes the numbers checked here: the full chart is centred at
(240, 240) with radius 170 in viewBox "-70 20 620 420", the small multiples at (110, 110) with
radius 80; axis 0 points up and the rest follow clockwise; 0 is the centre and 1 the edge. An
axis with no score is a gap in the outline, never a 0.

Skipped when Node is not installed; the module has no DOM and no imports.
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


def _geom(expr: str):
    uri = json.dumps((STATIC / "evals-geom.js").as_uri())
    script = f"import({uri}).then(m => console.log(JSON.stringify({expr})))"
    out = subprocess.run(  # nosec B603 - fixed argv
        [NODE, "--input-type=module", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_frames_match_the_canvas() -> None:
    assert _geom("m.FULL") == {"cx": 240, "cy": 240, "r": 170, "viewBox": "-70 20 620 420"}
    assert _geom("m.MINI") == {"cx": 110, "cy": 110, "r": 80, "viewBox": "20 20 180 175"}


def test_polar_points_match_the_canvas_perfect_outline() -> None:
    # The canvas' dotted perfect outline for seven axes.
    want = "240.0,70.0 372.9,134.0 405.7,277.8 313.8,393.2 166.2,393.2 74.3,277.8 107.1,134.0"
    assert _geom("m.ring(7, 1, m.FULL)") == want
    # The 0.5 grid ring and the small multiple's perfect outline.
    assert _geom("m.ring(7, 0.5, m.FULL)").startswith("240.0,155.0 306.5,187.0 322.9,258.9")
    assert _geom("m.ring(7, 1, m.MINI)") == (
        "110.0,30.0 172.5,60.1 188.0,127.8 144.7,182.1 75.3,182.1 32.0,127.8 47.5,60.1"
    )
    assert _geom("m.polar(0, 7, 0, m.FULL)") == [240, 240]
    assert _geom("m.polar(3, 7, 0.8, m.FULL)") == [299, 362.5]


def test_values_are_clamped_to_the_unit_range() -> None:
    assert _geom("m.polar(0, 4, 1.4, m.FULL)") == [240, 70]
    assert _geom("m.polar(0, 4, -0.2, m.FULL)") == [240, 240]


def test_outline_without_gaps_is_one_closed_shape() -> None:
    o = _geom("m.outline([1, 0.5, 1, 0.5], m.FULL)")
    assert o["gaps"] == []
    assert o["fills"] == ["240.0,70.0 325.0,240.0 240.0,410.0 155.0,240.0"]
    assert o["lines"] == ["240.0,70.0 325.0,240.0 240.0,410.0 155.0,240.0 240.0,70.0"]
    assert [v["i"] for v in o["vertices"]] == [0, 1, 2, 3]


def test_unscored_axes_are_gaps_never_zero() -> None:
    # Axis 2 has no score: the outline breaks there and the wedge around it stays empty.
    o = _geom("m.outline([1, 1, null, 1, 1], m.FULL)")
    assert o["gaps"] == [2]
    assert [v["i"] for v in o["vertices"]] == [0, 1, 3, 4]
    # One run of scored axes, walking from just after the gap round to just before it:
    # 3, 4, 0, 1.  It is drawn as an open line and filled as a fan from the centre.
    assert len(o["lines"]) == 1 and len(o["lines"][0].split()) == 4
    pts = o["lines"][0].split()
    assert pts[0] == _geom("m.polar(3, 5, 1, m.FULL).map(v => v.toFixed(1)).join(',')")
    assert pts[-1] == _geom("m.polar(1, 5, 1, m.FULL).map(v => v.toFixed(1)).join(',')")
    assert o["fills"] == ["240.0,240.0 " + " ".join(pts)]
    # No point of the shape sits at the centre of the gap axis.
    assert "240.0,240.0" not in o["lines"][0]


def test_outline_with_two_gaps_and_a_lone_axis() -> None:
    o = _geom("m.outline([0.5, null, 0.5, null, 0.5, 0.5], m.FULL)")
    assert o["gaps"] == [1, 3]
    # Runs: [2] alone (a vertex, no line), then [4, 5, 0].
    assert len(o["lines"]) == 1 and len(o["lines"][0].split()) == 3
    assert [v["i"] for v in o["vertices"]] == [0, 2, 4, 5]
    assert len(o["fills"]) == 1


def test_outline_with_nothing_scored() -> None:
    o = _geom("m.outline([null, null, null], m.FULL)")
    assert o == {"fills": [], "lines": [], "vertices": [], "gaps": [0, 1, 2]}


def test_label_anchor_follows_the_side_of_the_chart() -> None:
    got = _geom("[0, 1, 2, 3, 4, 5, 6].map(i => m.labelAt(i, 7, m.FULL).anchor)")
    assert got == ["middle", "start", "start", "start", "end", "end", "end"]
    top = _geom("m.labelAt(0, 7, m.FULL)")
    assert top["x"] == 240 and top["y"] < 70  # above the outline
    bottom = _geom("m.labelAt(3, 7, m.FULL)")
    assert bottom["y"] > 393.2  # below the outline


def test_axis_list_labels_tips_and_registry_extras() -> None:
    metrics = {
        "found_at_1": "Share of questions with a reference passage at rank 1.",
        "context_precision": "Judge: are the useful passages ranked first?",
        "error_rate": "Share of questions whose retrieval failed.",
        "fresh_share": "Share of questions whose reference chunks still exist.",
        "answer_overlap": "Token overlap with the reference answer.",
    }
    keys = ["found_at_1", "judge_precision", "no_errors", "fresh", "answer_overlap", "mrr"]
    got = _geom(f"m.axisList({json.dumps(keys)}, {json.dumps(metrics)})")
    assert [a["key"] for a in got] == keys
    assert [a["label"] for a in got] == [
        "Found at 1",
        "Judge precision",
        "No errors",
        "Fresh",
        "Answer overlap",
        "MRR",
    ]
    assert [a["short"] for a in got] == ["F1", "JP", "OK", "FR", "AO", "MRR"]
    assert got[0]["tip"] == "Share of questions with a reference passage at rank 1."
    assert got[1]["tip"] == "context_precision: Judge: are the useful passages ranked first?"
    assert (
        got[2]["tip"]
        == "One minus error_rate. error_rate: Share of questions whose retrieval failed."
    )
    assert got[4]["tip"] == "Token overlap with the reference answer."
    assert got[5]["tip"] == "Mean reciprocal rank of the first reference passage."  # built-in


def test_format_and_low_marks() -> None:
    assert _geom("[m.f2(0.734), m.f2(1), m.f2(null), m.f2(undefined)]") == [
        "0.73",
        "1.00",
        "not scored",
        "not scored",
    ]
    assert _geom("[m.isLow(0.69), m.isLow(0.7), m.isLow(null)]") == [True, False, False]


def test_ring_labels_sit_between_the_last_and_first_axis() -> None:
    # Plan task t18: the 0.5 / 1.0 ring labels moved off axis 0 (where a data vertex sits) into
    # the wedge between the last axis and the first, on the ring's edge.
    half = _geom("m.ringLabelAt(0.5, 7, m.FULL)")
    assert half == {"x": 206.8, "y": 175.4, "anchor": "middle"}
    one = _geom("m.ringLabelAt(1, 7, m.FULL)")
    assert one["x"] < 240 and one["y"] < half["y"]
    # Never on axis 0 (x = cx), for any axis count.
    xs = _geom("[3, 4, 5, 6, 7, 10].map(n => m.ringLabelAt(0.5, n, m.FULL).x)")
    assert all(x < 240 for x in xs)
