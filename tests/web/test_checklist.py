"""The reader's to-do checklist parser (``parseChecklist`` in ``web/static/checklist.js``, #11).

Runs the real module under Node when it is installed; the module is pure (no DOM).
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 - fixed argv
from pathlib import Path

import pytest

READER = Path(__file__).resolve().parents[2] / "culture_library" / "web" / "static" / "checklist.js"
NODE = shutil.which("node")

TODO = """<!-- todo:rec-2026-10-25 -->
# To do

- [ ] buy milk
- [x] call Ann <!-- todo:item-2 -->
* [X] book the room
plain note line

## Later
- [ ] write the plan
"""


def _parse(text: str) -> list:
    script = (
        f"import({json.dumps(READER.as_uri())}).then(m => "
        f"console.log(JSON.stringify(m.parseChecklist({json.dumps(text)}))))"
    )
    out = subprocess.run(  # nosec B603 - fixed argv
        [NODE, "--input-type=module", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_checklist_lines_headings_notes_and_hidden_comments() -> None:
    assert _parse(TODO) == [
        {"type": "heading", "text": "To do"},
        {"type": "check", "done": False, "text": "buy milk"},
        {"type": "check", "done": True, "text": "call Ann"},
        {"type": "check", "done": True, "text": "book the room"},
        {"type": "note", "text": "plain note line"},
        {"type": "heading", "text": "Later"},
        {"type": "check", "done": False, "text": "write the plan"},
    ]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_multiline_comment_is_hidden() -> None:
    assert _parse("<!-- a\nb -->\n- [ ] x\n") == [{"type": "check", "done": False, "text": "x"}]
