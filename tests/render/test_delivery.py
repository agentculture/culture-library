"""Delivery-JSON rendering tests for the devague renderer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from culture_library.render.devague import detect_kind, render

HOME_GIT = Path.home() / "git"
EVIL = "<script>alert(1)</script>"


def _delivery() -> dict:
    return {
        "schema_version": 2,
        "plan_slug": "p",
        "created": "2026-10-30",
        "deviations": [
            {
                "id": "d1",
                "what": EVIL,
                "task_ref": "t1",
                "reason": "because",
                "affects": ["c2", "c41"],
                "status": "approved",
                "classification": "acceptable",
            }
        ],
        "evidence": [
            {
                "id": "e1",
                "obligation_ref": "o1",
                "test_ref": "tests/x.py::t",
                "behavior_text": EVIL,
                "evidence_type": "automated",
                "strength": "execution",
                "strength_basis": "pytest ran",
                "outcome": "partial",
                "status": "confirmed",
            }
        ],
        "deltas": [
            {
                "id": "b1",
                "kind": "amended",
                "behavior_text": EVIL,
                "caused_by": ["c39"],
                "evidence_refs": ["e7"],
                "status": "proposed",
            }
        ],
    }


def test_detects_delivery():
    assert detect_kind(_delivery()) == "delivery"
    assert detect_kind({"plan_slug": "p", "deviations": []}) == "delivery"
    assert detect_kind({"plan_slug": "p"}) is None
    assert detect_kind({"plan_slug": "p", "tasks": []}) == "plan"


def test_delivery_renders_sections_escaped():
    out = render(_delivery())
    assert "dv-raw" not in out
    assert "devague-delivery" in out
    for needle in [
        "Deviations",
        "Evidence",
        "Behavioral deltas",
        "execution",
        "partial",
        "amended",
        "acceptable",
        "c41",
        "tests/x.py::t",
    ]:
        assert needle in out
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_schema1_delivery_with_only_deviations():
    out = render({"schema_version": 1, "plan_slug": "p", "deviations": [{"id": "d1", "what": "w"}]})
    assert "dv-raw" not in out
    assert "Deviations" in out


def test_odd_delivery_values_do_not_raise():
    out = render({"plan_slug": "p", "deviations": [None, 3, {"affects": "x"}], "evidence": "no"})
    assert out.startswith("<article")


def test_unknown_schema_delivery_is_raw_with_notice():
    out = render({"schema_version": 9, "plan_slug": "p", "deviations": []})
    assert "dv-raw" in out and "dv-notice" in out


def _corpus_files() -> list[Path]:
    if not HOME_GIT.is_dir():
        return []
    return sorted(
        p
        for p in HOME_GIT.glob("*/.devague/deliveries/*.json")
        if "worktrees" not in str(p.relative_to(HOME_GIT)).lower()
    )


def test_home_git_delivery_corpus_renders_without_raw():
    files = _corpus_files()
    if not files:
        pytest.skip("no ~/git/*/.devague/deliveries corpus on this machine")
    rendered = 0
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out = render(data)
        assert "dv-raw" not in out, path
        assert "devague-delivery" in out, path
        rendered += 1
    print(f"delivery corpus: rendered={rendered} of {len(files)}")
    assert rendered > 0
