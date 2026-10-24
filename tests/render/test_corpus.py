"""Corpus + fixture tests for the devague renderer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from culture_library.render.devague import compute_waves, render

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_FILES = sorted(FIXTURES.glob("*.json"))
HOME_GIT = Path.home() / "git"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_fixtures_cover_every_schema_and_unversioned():
    names = {p.stem for p in FIXTURE_FILES}
    for v in [1, 2, 3, 4, 5, 6]:
        assert f"frame_v{v}" in names
    assert {"frame_vunversioned", "plan_vunversioned"} <= names
    assert {"plan_v1", "plan_v2", "plan_v3", "plan_v4", "plan_v5"} <= names


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.stem)
def test_fixture_renders(path):
    data = _load(path)
    out = render(data)
    assert out.startswith("<article")
    assert "dv-raw" not in out
    kind = "plan" if "tasks" in data else "frame"
    assert f"devague-{kind}" in out
    if kind == "frame":
        assert "dv-claim" in out
        assert data["claims"][0]["id"] in out
    else:
        assert "Plan waves" in out
        assert "Wave 0" in out


def test_frame_v6_has_all_sections():
    out = render(_load(FIXTURES / "frame_v6_rich.json"))
    for needle in [
        "Claims",
        "Honesty conditions",
        "confirmed",
        "Scope entries",
        "Obligations",
        "Lapses",
    ]:
        assert needle in out


def test_questions_and_scope_render():
    out = render(_load(FIXTURES / "frame_v6_rich.json"))
    assert "Hard questions" in out
    assert "Open vagueness" in out
    assert "Scope entries" in out


def test_plan_sections():
    out = render(_load(FIXTURES / "plan_v5_rich.json"))
    assert "Coverage targets" in out
    assert "Risks" in out
    assert "Obligations" in out


def test_synthetic_schema_7_is_raw_with_notice():
    out = render({"schema_version": 7, "slug": "x", "claims": [{"id": "c1", "text": "<b>hi</b>"}]})
    assert "dv-raw" in out
    assert "dv-notice" in out
    assert "<pre>" in out
    assert "newer than the supported 6" in out
    assert "<b>" not in out


def test_non_int_and_unrecognised_shapes_fall_back_to_raw():
    assert "dv-notice" in render({"schema_version": "9", "claims": []})
    assert "dv-notice" in render({"hello": "world"})
    assert "dv-notice" in render([1, 2])


def test_everything_is_escaped():
    evil = "<script>alert(1)</script>"
    frame = {
        "schema_version": 6,
        "title": evil,
        "slug": evil,
        "claims": [
            {
                "id": evil,
                "kind": evil,
                "text": evil,
                "instruction": evil,
                "honesty_conditions": [{"id": evil, "text": evil}],
                "hard_questions": [{"id": evil, "text": evil, "resolution": evil}],
            }
        ],
        "scope_entries": [{"id": evil, "surface": evil, "finding": evil, "seeds": [evil]}],
        "open_vagueness": [{"id": evil, "text": evil}],
        "obligations": [{"id": evil, "seam": evil, "behavior": evil}],
        "lapses": [{"id": evil, "what": evil}],
    }
    plan = {
        "schema_version": 5,
        "title": evil,
        "targets": [{"id": evil, "text": evil}],
        "tasks": [
            {"id": "t1", "summary": evil, "acceptance_criteria": [evil], "deps": [evil]},
        ],
        "risks": [{"id": evil, "text": evil}],
    }
    for data in (frame, plan):
        out = render(data)
        assert "<script>" not in out
        assert "&lt;script&gt;" in out


def test_tolerates_missing_and_odd_keys():
    assert render({"claims": []}).startswith("<article")
    assert render({"schema_version": 3, "claims": [None, 5, {"text": None}], "x": 1})
    assert render({"tasks": [{"deps": "nope"}, {"id": "a", "deps": ["a", "zzz"]}]})


def test_waves_follow_dependencies_and_survive_cycles():
    tasks = [
        {"id": "t1", "deps": []},
        {"id": "t2", "deps": ["t1"]},
        {"id": "t3", "deps": ["t1"]},
        {"id": "t4", "deps": ["t2", "t3"]},
    ]
    assert [[t["id"] for t in w] for w in compute_waves(tasks)] == [["t1"], ["t2", "t3"], ["t4"]]
    cyc = [{"id": "a", "deps": ["b"]}, {"id": "b", "deps": ["a"]}]
    assert len(compute_waves(cyc)) == 1


def _corpus_files():
    if not HOME_GIT.is_dir():
        return []
    files = []
    for sub in ("frames", "plans"):
        files += HOME_GIT.glob(f"*/.devague/{sub}/*.json")
    return sorted(files)


def test_home_git_corpus_renders():
    files = _corpus_files()
    if not files:
        pytest.skip("no ~/git/*/.devague corpus on this machine")
    rendered = raw = 0
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out = render(data)  # must never raise
        assert isinstance(out, str)
        assert out
        if "dv-raw" in out:
            raw += 1
        else:
            rendered += 1
    print(f"corpus: rendered={rendered} raw={raw} total={len(files)}")
    assert rendered > 0
