import json
import re
from pathlib import Path

import pytest

from culture_library.core.devague_text import text_for

ROOT = Path(__file__).resolve().parents[2]
CORPUS = sorted((ROOT / ".devague").glob("*/*.json"))
BAD = re.compile(r'^[\s\[\]{}(),:"\']*$|^\s*"?[A-Za-z_]+"?\s*:\s*[\[{]?\s*$')


def test_corpus_present():
    assert len(CORPUS) >= 10


@pytest.mark.parametrize("p", CORPUS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_json_noise_lines(p):
    out = text_for(str(p), json.loads(p.read_text()))
    assert out.strip()
    for line in out.splitlines():
        assert line.strip(), line
        assert not BAD.match(line), line
        assert not line.lstrip().startswith(('{"', '["'))


def test_frame():
    data = {
        "title": "T",
        "claims": [
            {
                "id": "c1",
                "kind": "requirement",
                "text": "it works",
                "honesty_conditions": [{"id": "h1", "text": "unless broken"}],
                "hard_questions": [{"id": "q1", "text": "why?", "resolution": "because"}],
            }
        ],
        "scope_entries": [{"id": "s1", "surface": "x.py", "finding": "found it"}],
    }
    out = text_for("f.json", data)
    for s in ("T", "it works", "unless broken", "why?", "because", "found it"):
        assert s in out


def test_plan():
    data = {
        "title": "P",
        "tasks": [{"id": "t1", "summary": "do it", "acceptance_criteria": ["is done"]}],
        "risks": [{"id": "r1", "text": "might fail"}],
    }
    out = text_for("p.json", data)
    for s in ("do it", "is done", "might fail"):
        assert s in out


def test_delivery():
    data = {
        "plan_slug": "p",
        "deviations": [{"id": "d1", "what": "changed", "reason": "needed"}],
        "evidence": [{"id": "e1", "behavior_text": "behaves"}],
        "deltas": [{"id": "b1", "kind": "added", "behavior_text": "new thing"}],
    }
    out = text_for("d.json", data)
    for s in ("changed", "needed", "behaves", "new thing"):
        assert s in out


def test_unknown_shape_strings_only():
    out = text_for("x.json", {"alpha": {"beta": ["hello world", 3, None]}, "n": 1})
    assert out.strip() == "hello world"
    assert "alpha" not in out
    assert "{" not in out
    assert text_for("x.json", [1, 2]).strip() == ""
