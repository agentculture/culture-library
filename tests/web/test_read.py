"""Reader content endpoints: item text, repo-doc rendering, card excerpts (plan task t15).

Content is untrusted (claim c38): the item endpoints return extracted plain text only, and
the devague fragment is HTML only because :func:`culture_library.render.devague.render`
escapes every input string.
"""

from __future__ import annotations

import json

import pytest

from .conftest import OWNER, READER, Site, make_epub

HOSTILE_CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Ch</title>
<script>alert('head')</script><style>body{display:none}</style></head>
<body onload="alert('body')">
<h1>First chapter</h1>
<p>Plain words stay.</p>
<script type="text/javascript">document.cookie='x'; alert('inline')</script>
<p><img src="x.png" onerror="alert('img')"/>An image tried to run code.</p>
<p><a href="javascript:alert('link')">A link that tried to run code</a></p>
<svg><script>alert('svg')</script></svg>
<h2>Second part</h2>
<ul><li>One item</li><li>Two items</li></ul>
</body></html>"""
FORBIDDEN = ("<script", "alert(", "onerror", "onload", "javascript:", "document.cookie", "<")


@pytest.fixture
def hostile(site: Site) -> dict:
    data = make_epub(
        {
            "OEBPS/c1.xhtml": HOSTILE_CHAPTER,
            "OEBPS/nav.xhtml": "<html><body><nav><p>Table of contents</p></nav></body></html>",
        }
    )
    return site.upload(data, "hostile.epub", title="Hostile book", visibility="public")


def _all_strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _all_strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _all_strings(v)]
    return []


def test_epub_text_is_plain_and_runs_nothing(site: Site, hostile: dict) -> None:
    r = site.c.get(f"/api/library/{hostile['id']}/text")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["format"] == "epub" and body["kind"] == "book"
    strings = _all_strings({"text": body["text"], "sections": body["sections"]})
    joined = "\n".join(strings)
    for bad in FORBIDDEN:
        assert bad not in joined, bad
    assert "Plain words stay." in body["text"]
    assert "A link that tried to run code" in body["text"]


def test_epub_sections_split_by_heading(site: Site, hostile: dict) -> None:
    sections = site.c.get(f"/api/library/{hostile['id']}/text").json()["sections"]
    assert [s["heading"] for s in sections] == ["First chapter", "Second part"]
    assert sections[0]["paragraphs"] == [
        "Plain words stay.",
        "An image tried to run code.",
        "A link that tried to run code",
    ]
    assert sections[1]["paragraphs"] == ["One item", "Two items"]
    assert all("Table of contents" not in p for s in sections for p in s["paragraphs"])


def test_hebrew_title_and_text_come_back_intact(site: Site) -> None:
    title = "ספר בדיקה: שלום עולם"
    text = "שלום עולם. זו פסקה בעברית עם מילה English באמצע.\n\nפסקה שנייה."
    item = site.upload(text.encode("utf-8"), "hebrew.txt", title=title, visibility="public")
    assert site.c.get(f"/api/library/{item['id']}").json()["title"] == title
    body = site.c.get(f"/api/library/{item['id']}/text").json()
    assert body["title"] == title
    assert body["text"] == text
    assert body["sections"] is None
    epub = make_epub({"c.xhtml": f"<html><body><h1>{title}</h1><p>{text}</p></body></html>"})
    e = site.upload(epub, "he.epub", title=title, visibility="public")
    sections = site.c.get(f"/api/library/{e['id']}/text").json()["sections"]
    assert sections[0]["heading"] == title
    assert sections[0]["paragraphs"][0].startswith("שלום עולם.")


def test_markdown_text_has_no_markup(site: Site) -> None:
    md = b'# Title\n\nSome *bold* text with <b onclick="x()">raw html</b> and [a](javascript:x).\n'
    item = site.upload(md, "note.md", visibility="public")
    text = site.c.get(f"/api/library/{item['id']}/text").json()["text"]
    assert "onclick" not in text and "<b" not in text and "javascript:" not in text
    assert "raw html" in text


def test_private_item_text_is_404_for_non_private_caller(site: Site) -> None:
    item = site.upload(b"secret words", "s.txt", visibility="private")
    assert site.c.get(f"/api/library/{item['id']}/text").status_code == 200
    site.as_(READER)
    r = site.c.get(f"/api/library/{item['id']}/text")
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"
    assert "secret" not in r.text
    assert site.c.get("/api/library/999/text").status_code == 404


def test_archived_item_text_is_404(site: Site) -> None:
    item = site.upload(b"old words", "o.txt", visibility="public")
    assert site.c.delete(f"/api/library/{item['id']}").status_code == 204
    assert site.c.get(f"/api/library/{item['id']}/text").status_code == 404


def test_text_needs_read_scope(site: Site) -> None:
    from culture_library.server.auth import Principal

    item = site.upload(b"words", "w.txt", visibility="public")
    site.as_(Principal("n", frozenset()))
    assert site.c.get(f"/api/library/{item['id']}/text").status_code == 403


HOSTILE_FRAME = {
    "slug": "evil",
    "title": "<script>alert('title')</script>",
    "schema_version": 6,
    "status": "drafting",
    "claims": [
        {
            "id": "c1",
            "kind": "announcement",
            "status": "confirmed",
            "text": '<img src=x onerror="alert(1)"> and <a href="javascript:alert(2)">x</a>',
            "hard_questions": [
                {"id": "q1", "text": "<script>q()</script>", "resolution": "<b>no</b>"}
            ],
        }
    ],
    "scope_entries": [{"id": "s1", "surface": "<svg onload=alert(3)>", "finding": "f"}],
}


def test_repodoc_rendered_frame_is_escaped(site: Site) -> None:
    doc = site.push_doc(".devague/frames/evil.json", json.dumps(HOSTILE_FRAME))
    r = site.c.get(f"/api/repodocs/{doc['id']}/rendered")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["format"] == "html"
    assert body["title"] == HOSTILE_FRAME["title"]  # data, shown with textContent
    html = body["html"]
    assert html.startswith('<article class="devague devague-frame"')
    for raw in ("<script", "<img", "<svg", "<a ", "<b>"):
        assert raw not in html, raw
    assert "&lt;script&gt;" in html and "&lt;img src=x onerror=" in html


def test_repodoc_rendered_plan_and_unknown_json(site: Site) -> None:
    plan = {"slug": "p", "schema_version": 6, "tasks": [{"id": "t1", "summary": "<i>x</i>"}]}
    doc = site.push_doc(".devague/plans/p.json", json.dumps(plan))
    html = site.c.get(f"/api/repodocs/{doc['id']}/rendered").json()["html"]
    assert "devague-plan" in html and "<i>" not in html and "&lt;i&gt;x&lt;/i&gt;" in html
    other = site.push_doc(".devague/reviews/x.json", '{"x": "<script>"}')
    body = site.c.get(f"/api/repodocs/{other['id']}/rendered").json()
    assert body["format"] == "html" and "<script" not in body["html"]
    broken = site.push_doc(".devague/frames/broken.json", "{not json <script>")
    body = site.c.get(f"/api/repodocs/{broken['id']}/rendered").json()
    assert body["format"] == "text" and body["text"] == "{not json <script>"
    assert "html" not in body


def test_repodoc_rendered_markdown_is_plain_text(site: Site) -> None:
    md = "# Gwen is pocketchat\n\n> The announcement\n\n## Requirements\n\n- one\n"
    doc = site.push_doc("docs/specs/2026-10-23-gwen.md", md)
    body = site.c.get(f"/api/repodocs/{doc['id']}/rendered").json()
    assert body == {
        "doc_id": doc["id"],
        "repo": "github.com/agentculture/devague",
        "path": "docs/specs/2026-10-23-gwen.md",
        "kind": "spec",
        "format": "text",
        "title": "Gwen is pocketchat",
        "text": md,
    }


def test_repodoc_rendered_visibility(site: Site) -> None:
    doc = site.push_doc("docs/specs/secret.md", "# Secret\n", visibility="private")
    assert site.c.get(f"/api/repodocs/{doc['id']}/rendered").status_code == 200
    site.as_(READER)
    r = site.c.get(f"/api/repodocs/{doc['id']}/rendered")
    assert r.status_code == 404 and "Secret" not in r.text
    assert site.c.get("/api/repodocs/999/rendered").status_code == 404


def test_excerpts_for_cards(site: Site, hostile: dict) -> None:
    txt = site.upload("שלום עולם, a Hebrew note".encode(), "he.txt", visibility="public")
    secret = site.upload(b"private words", "p.txt", visibility="private")
    spec = site.push_doc(
        "docs/specs/2026-10-23-gwen.md",
        "# Gwen is pocketchat, transcripts as knowledge\n\n> Gwen's page is pocketchat\n",
    )
    frame = site.push_doc(".devague/frames/evil.json", json.dumps(HOSTILE_FRAME))
    ids = f"{hostile['id']},{txt['id']},{secret['id']},999"
    r = site.c.get("/api/excerpts", params={"items": ids, "docs": f"{spec['id']},{frame['id']}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"][str(txt["id"])] == "שלום עולם, a Hebrew note"
    assert body["items"][str(hostile["id"])].startswith("First chapter")
    assert str(secret["id"]) in body["items"]  # OWNER sees private
    assert "999" not in body["items"]
    assert body["docs"][str(spec["id"])] == {
        "title": "Gwen is pocketchat, transcripts as knowledge",
        "excerpt": "Gwen's page is pocketchat",
    }
    assert body["docs"][str(frame["id"])]["title"] == HOSTILE_FRAME["title"]
    assert body["docs"][str(frame["id"])]["excerpt"].startswith("<img src=x")  # text, not HTML
    for bad in ("onerror", "<script"):
        assert bad not in body["items"][str(hostile["id"])]
    site.as_(READER)
    hidden = site.c.get("/api/excerpts", params={"items": str(secret["id"])}).json()
    assert hidden["items"] == {}


def test_excerpts_validate_ids(site: Site) -> None:
    assert site.c.get("/api/excerpts", params={"items": "1,x"}).status_code == 400
    too_many = ",".join(str(i) for i in range(1, 300))
    assert site.c.get("/api/excerpts", params={"items": too_many}).status_code == 400
    assert site.c.get("/api/excerpts").json() == {"items": {}, "docs": {}}


def test_owner_fixture_is_private() -> None:
    assert OWNER.private


_NEWS_CH = (
    '<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Daily newspaper</h1><p>2026-10-23</p>'
    "<h2>Jetson optimization</h2><p>Models | Jetson AI Lab: benchmarks.</p>"
    "<h2>Isaac ROS</h2><p>Second item.</p></body></html>"
)
_NAV = (
    '<html xmlns="http://www.w3.org/1999/xhtml"><body><nav><h1>Contents</h1>'
    "<p>Jetson optimization</p><p>Isaac ROS</p></nav></body></html>"
)


def _newspaper(site: Site) -> dict:
    data = make_epub({"OEBPS/nav.xhtml": _NAV, "OEBPS/s01.xhtml": _NEWS_CH})
    return site.upload(
        data, "n.epub", title="Daily newspaper 2026-10-23", kind="newspaper", visibility="public"
    )


def test_masthead_section_is_dropped(site: Site) -> None:
    item = _newspaper(site)
    sections = site.c.get(f"/api/library/{item['id']}/text").json()["sections"]
    assert [s["heading"] for s in sections] == ["Jetson optimization", "Isaac ROS"]


def test_masthead_kept_when_it_has_real_content() -> None:
    from culture_library.server.routes_read import drop_masthead

    real = [{"heading": "Daily newspaper", "paragraphs": ["2026-10-23", "A real paragraph."]}]
    assert drop_masthead(real, "Daily newspaper 2026-10-23") == real
    other = [{"heading": "Chapter one", "paragraphs": ["2026-10-23"]}]
    assert drop_masthead(other, "Daily newspaper 2026-10-23") == other


def test_epub_excerpt_skips_nav_and_masthead(site: Site) -> None:
    item = _newspaper(site)
    got = site.c.get("/api/excerpts", params={"items": str(item["id"])}).json()
    assert got["items"][str(item["id"])] == (
        "Jetson optimization, Isaac ROS. Models | Jetson AI Lab: benchmarks."
    )


@pytest.mark.parametrize("how", ["crc", "method", "encrypted", "deflate"])
def test_text_of_a_stored_corrupt_epub_degrades_not_500(site: Site, how: str) -> None:
    from tests.core.test_ingest import corrupt_epub

    # stored by a path that bypasses ingest (e.g. a migration), so the bytes were never checked
    item = site.store.add_item(corrupt_epub(how), kind="book", title="Bad", format="epub").item
    r = site.c.get(f"/api/library/{item.id}/text")
    assert r.status_code == 200, r.text
    assert r.json()["reason"] == "corrupt_archive" and r.json()["text"] == ""
    ex = site.c.get("/api/excerpts", params={"items": str(item.id)})
    assert ex.status_code == 200 and ex.json()["items"] == {str(item.id): ""}
