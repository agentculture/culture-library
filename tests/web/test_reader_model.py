"""The reader's pure decisions (``web/static/reader-model.js``, plan task t21).

Which version a repo doc opens on, how a design is presented, what a spec page says about its
PR and code paths, the removed notice, and the safe Markdown block model for reviews and
questions. Runs the real module under Node when it is installed; the module has no DOM.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 - fixed argv
from pathlib import Path
from typing import Any

import pytest

STATIC = Path(__file__).resolve().parents[2] / "culture_library" / "web" / "static"
MODEL = STATIC / "reader-model.js"
READER = STATIC / "reader.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _call(fn: str, *args: Any) -> Any:
    argv = ", ".join(json.dumps(a) for a in args)
    script = (
        f"import({json.dumps(MODEL.as_uri())}).then(m => "
        f"console.log(JSON.stringify(m.{fn}({argv}) ?? null)))"
    )
    out = subprocess.run(  # nosec B603 - fixed argv
        [NODE, "--input-type=module", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


CUR = {"commit": "a" * 40}


def _meta(draft: Any, current: Any = CUR) -> dict:
    return {"draft": draft, "current": current}


@needs_node
def test_ahead_draft_shows_by_default_with_working_badge_and_a_way_to_current() -> None:
    v = _call("chooseView", _meta({"ahead": True}), None)
    assert v == {"version": "working", "working": True, "toggle": "current", "badge": True}
    back = _call("chooseView", _meta({"ahead": True}), "current")
    assert back == {"version": "a" * 40, "working": False, "toggle": "working", "badge": False}
    again = _call("chooseView", _meta({"ahead": True}), "working")
    assert again["working"] is True


@needs_node
def test_not_ahead_or_no_draft_shows_current_as_today() -> None:
    for draft in (None, {"ahead": False}):
        v = _call("chooseView", _meta(draft), None)
        assert v == {"version": None, "working": False, "toggle": None, "badge": False}
        # A stale "working" choice cannot show a draft that is not ahead.
        assert _call("chooseView", _meta(draft), "working")["working"] is False


@needs_node
def test_draft_only_doc_has_no_committed_version_to_go_back_to() -> None:
    v = _call("chooseView", _meta({"ahead": True}, None), "current")
    assert v == {"version": "working", "working": True, "toggle": None, "badge": True}


@needs_node
def test_design_presentation_by_file_type() -> None:
    cases = {
        "designs/a.PNG": "image",
        "designs/a.jpg": "image",
        "designs/a.webp": "image",
        "designs/a.svg": "image",  # an <img>, never inline markup
        "designs/a.html": "html",
        "designs/a.htm": "html",
        "designs/a.pdf": "download",
        "designs/a.bin": "download",
    }
    for path, mode in cases.items():
        assert _call("designMode", path) == mode, path


@needs_node
def test_sandbox_never_allows_same_origin_or_scripts() -> None:
    sandbox = _call("prototypeSandbox")
    assert "allow-same-origin" not in sandbox
    assert "allow-scripts" not in sandbox


@needs_node
def test_spec_links_model() -> None:
    empty = {
        "doc_id": 1,
        "pr": None,
        "merge_commit": None,
        "code_paths": [],
        "at_commit": None,
        "at_committed_at": None,
        "trailer_commits": [],
    }
    assert _call("specLinksModel", empty) is None
    assert _call("specLinksModel", None) is None
    full = dict(
        empty,
        pr={"url": "https://github.com/o/r/pull/7", "number": 7},
        merge_commit="b" * 40,
        code_paths=["x/a.py", "x/b.py"],
        at_commit="c" * 40,
        at_committed_at="2026-10-30T09:15:00Z",
    )
    m = _call("specLinksModel", full)
    assert m["pr"] == {"href": "https://github.com/o/r/pull/7", "label": "#7"}
    assert m["merge"] == "b" * 7
    assert m["paths"] == ["x/a.py", "x/b.py"]
    assert m["computed"] == "computed at ccccccc (2026-10-30)"


@needs_node
def test_spec_links_pr_without_safe_url_is_text_only() -> None:
    base = {"merge_commit": None, "code_paths": [], "at_commit": None, "at_committed_at": None}
    m = _call("specLinksModel", dict(base, pr={"url": "javascript:alert(1)", "number": 9}))
    assert m["pr"] == {"href": None, "label": "#9"}
    m = _call("specLinksModel", dict(base, pr={"url": None, "number": 9}))
    assert m["pr"] == {"href": None, "label": "#9"}
    only_merge = _call("specLinksModel", dict(base, merge_commit="d" * 40))
    assert only_merge["pr"] is None
    assert only_merge["computed"] is None


@needs_node
def test_removed_notice() -> None:
    gone = {"removed": True, "removed_at": "2026-10-29T10:00:00Z", "removed_commit": "e" * 40}
    notice = _call("removedNotice", gone)
    assert notice == "Removed at eeeeeee (2026-10-29). History stays readable."
    assert _call("removedNotice", {"removed": False}) is None
    assert _call("removedNotice", None) is None


@needs_node
def test_markdown_blocks_are_data_not_markup() -> None:
    md = (
        "# Review\n\nA **bold** and `code` <b>tag</b>.\n\n- one\n- two\n\n1. a\n2. b\n\n"
        "```\nx < y\n```\n> quoted\n"
    )
    blocks = _call("markdownBlocks", md)
    assert blocks[0] == {"type": "h", "level": 1, "inline": [{"t": "text", "s": "Review"}]}
    assert blocks[1]["type"] == "p"
    assert blocks[1]["inline"] == [
        {"t": "text", "s": "A "},
        {"t": "b", "s": "bold"},
        {"t": "text", "s": " and "},
        {"t": "code", "s": "code"},
        {"t": "text", "s": " <b>tag</b>."},
    ]
    assert blocks[2]["type"] == "ul"
    assert len(blocks[2]["items"]) == 2
    assert blocks[3]["type"] == "ol"
    assert len(blocks[3]["items"]) == 2
    assert blocks[4] == {"type": "code", "text": "x < y"}
    assert blocks[5]["type"] == "quote"


def test_reader_wires_the_model_and_never_embeds_prototypes_unsandboxed() -> None:
    src = READER.read_text()
    assert "./reader-model.js" in src
    assert "srcdoc" in src
    assert "sandbox" in src
    assert "allow-same-origin" not in src
