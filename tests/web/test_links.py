"""Source links in the reader's EPUB sections (#18).

``_SectionParser`` keeps an ``<a>`` whose href is an absolute http(s) URL as a parallel
``links`` list per section (``links[i]`` for ``paragraphs[i]``); ``paragraphs`` stay plain
strings, so search, the index and older readers are unaffected. The reader turns those links
into ``<a>`` elements with DOM APIs (``web/static/links.js``, run under Node here).
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 - fixed argv
from pathlib import Path

import pytest

from culture_library.server.routes_read import _sections_of

from .conftest import Site, make_epub

LINKS_JS = Path(__file__).resolve().parents[2] / "culture_library" / "web" / "static" / "links.js"
NODE = shutil.which("node")


def _one(markup: str) -> dict:
    sections = _sections_of(f"<html><body>{markup}</body></html>")
    assert len(sections) == 1, sections
    return sections[0]


# ------------------------------------------------------------------ parser


@pytest.mark.parametrize(
    "href",
    [
        "https://x.example/a",
        "http://x.example/a",
        "HTTPS://X.example/a?q=1#frag",
        "  https://x.example/a  ",
    ],
)
def test_absolute_http_links_are_kept(href: str) -> None:
    s = _one(f'<p>summary text <a href="{href}">Source</a></p>')
    assert s["paragraphs"] == ["summary text Source"]
    assert s["links"] == [[{"text": "Source", "href": href.strip()}]]


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        " javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "relative/page.html",
        "/absolute/path",
        "#fragment",
        "//x.example/a",
        "mailto:a@x.example",
        "ftp://x.example/a",
        "https:x.example",
        "https:///nohost",
        "https:\\\\x.example",
        "https://x.example/a b",
        "https://x.example/\x00",
        "",
    ],
)
def test_other_hrefs_stay_plain_text(href: str) -> None:
    s = _one(f"<p>summary text <a href={json.dumps(href)}>Source</a></p>")
    assert s["paragraphs"] == ["summary text Source"]
    assert s["links"] == [[]]


def test_anchor_without_href_is_plain_text() -> None:
    s = _one('<p>a <a name="x">b</a> c</p>')
    assert s["paragraphs"] == ["a b c"]
    assert s["links"] == [[]]


def test_entities_in_href_are_decoded() -> None:
    s = _one('<p>See <a href="https://x.example/a?b=1&amp;c=2">it</a>.</p>')
    assert s["links"] == [[{"text": "it", "href": "https://x.example/a?b=1&c=2"}]]
    # an entity-encoded scheme is still refused once decoded
    s = _one('<p><a href="&#106;avascript:alert(1)">x</a></p>')
    assert s["links"] == [[]]
    s = _one('<p><a href="&#104;ttps://x.example/">x</a></p>')
    assert s["links"] == [[{"text": "x", "href": "https://x.example/"}]]


def test_markup_inside_a_and_a_inside_strong() -> None:
    s = _one(
        '<p>One <a href="https://x.example/1"><strong>Bold</strong> link</a> and '
        '<strong>see <a href="https://x.example/2">Two</a></strong> end</p>'
    )
    assert s["paragraphs"] == ["One Bold link and see Two end"]
    assert s["links"] == [
        [
            {"text": "Bold link", "href": "https://x.example/1"},
            {"text": "Two", "href": "https://x.example/2"},
        ]
    ]


def test_links_are_per_paragraph_and_parallel() -> None:
    s = _one(
        "<ul>"
        '<li>first <a href="https://a.example/">A</a></li>'
        "<li>no link here</li>"
        '<li>third <a href="https://c.example/">C</a> and <a href="https://d.example/">D</a></li>'
        "</ul>"
    )
    assert s["paragraphs"] == ["first A", "no link here", "third C and D"]
    assert s["links"] == [
        [{"text": "A", "href": "https://a.example/"}],
        [],
        [
            {"text": "C", "href": "https://c.example/"},
            {"text": "D", "href": "https://d.example/"},
        ],
    ]
    assert len(s["links"]) == len(s["paragraphs"])


def test_link_text_is_whitespace_collapsed_and_located_in_order() -> None:
    s = _one('<p>Source x <a href="https://a.example/">\n  Source \n</a></p>')
    assert s["paragraphs"] == ["Source x Source"]
    assert s["links"] == [[{"text": "Source", "href": "https://a.example/"}]]
    for i, para in enumerate(s["paragraphs"]):
        at = 0
        for link in s["links"][i]:
            found = para.find(link["text"], at)
            assert found >= 0
            at = found + len(link["text"])


def test_link_spanning_blocks_links_each_part_and_empty_links_are_dropped() -> None:
    s = _one(
        '<a href="https://a.example/"><p>Part one</p><p>Part two</p></a>'
        '<p><a href="https://b.example/"> </a>x</p>'
    )
    assert s["paragraphs"] == ["Part one", "Part two", "x"]
    assert s["links"] == [
        [{"text": "Part one", "href": "https://a.example/"}],
        [{"text": "Part two", "href": "https://a.example/"}],
        [],
    ]


def test_nested_anchor_keeps_the_outer_link() -> None:
    s = _one(
        '<p><a href="https://a.example/">out <a href="https://b.example/">in</a></a> after</p>'
    )
    assert s["paragraphs"] == ["out in after"]
    assert s["links"] == [[{"text": "out in", "href": "https://a.example/"}]]


def test_links_in_headings_and_skipped_bodies_are_not_recorded() -> None:
    sections = _sections_of(
        '<html><body><h1><a href="https://h.example/">Head</a></h1>'
        '<p>text<script><a href="https://s.example/">no</a></script></p></body></html>'
    )
    assert sections == [{"heading": "Head", "paragraphs": ["text"], "links": [[]]}]


def test_parser_output_never_carries_markup() -> None:
    s = _one('<p><a href="https://x.example/&lt;b&gt;">&lt;i&gt;x</a></p>')
    assert s["paragraphs"] == ["<i>x"]  # character data, as before: text, not markup
    assert s["links"] == [[{"text": "<i>x", "href": "https://x.example/<b>"}]]


# ------------------------------------------------------------------ API

_LINKED = (
    '<html xmlns="http://www.w3.org/1999/xhtml"><body><h2>Jetson</h2><ul>'
    '<li>summary text <a href="https://x.example/a">X</a></li>'
    '<li>bad <a href="javascript:alert(1)">J</a> <a href="data:text/html,x">D</a> '
    '<a href="rel/page.html">R</a> <a href="//x.example/p">P</a></li>'
    "</ul></body></html>"
)


def _plain(markup: str) -> str:
    """The same chapter with every anchor's attributes removed."""
    import re

    return re.sub(r"<a [^>]*>", "<a>", markup)


def test_text_route_returns_links_and_unchanged_paragraphs(site: Site) -> None:
    item = site.upload(
        make_epub({"OEBPS/s01.xhtml": _LINKED}),
        "n.epub",
        title="Daily newspaper",
        kind="newspaper",
        visibility="public",
    )
    body = site.c.get(f"/api/library/{item['id']}/text").json()
    (section,) = body["sections"]
    assert section["paragraphs"] == ["summary text X", "bad J D R P"]
    assert section["links"] == [[{"text": "X", "href": "https://x.example/a"}], []]
    plain = site.upload(
        make_epub({"OEBPS/s01.xhtml": _plain(_LINKED)}), "p.epub", visibility="public"
    )
    plain_body = site.c.get(f"/api/library/{plain['id']}/text").json()
    assert plain_body["sections"][0]["paragraphs"] == section["paragraphs"]
    assert plain_body["text"] == body["text"]


def test_indexed_body_and_search_of_a_linked_epub_are_unchanged(site: Site) -> None:
    from culture_library.core import ingest

    linked_data = make_epub({"OEBPS/s01.xhtml": _LINKED})
    plain_data = make_epub({"OEBPS/s01.xhtml": _plain(_LINKED)})
    linked = site.upload(linked_data, "l.epub", title="Linked", visibility="public")
    plain = site.upload(plain_data, "p.epub", title="Plain", visibility="public")
    with site.store.reader() as conn:
        rows = dict(
            conn.execute(
                "SELECT rowid, body FROM search_fts WHERE rowid IN (?, ?)",
                (linked["id"], plain["id"]),
            ).fetchall()
        )
    assert rows[linked["id"]] == rows[plain["id"]] == ingest.extract_text(plain_data, "epub")
    assert "https://" not in rows[linked["id"]]
    r = site.c.get("/api/search", params={"q": "summary", "mode": "fulltext"})
    hits = {h["item_id"]: h["snippet"] for h in r.json()["hits"]}
    assert set(hits) == {linked["id"], plain["id"]}
    assert hits[linked["id"]] == hits[plain["id"]]


# ------------------------------------------------------------------ reader (Node)


def _node(expr: str):
    script = (
        f"import({json.dumps(LINKS_JS.as_uri())}).then(m => {{ const r = ({expr}); "
        "console.log(JSON.stringify(r)); })"
    )
    out = subprocess.run(  # nosec B603 - fixed argv
        [NODE, "--input-type=module", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


@needs_node
def test_link_segments_split_at_link_text_in_order() -> None:
    text = "Source one: Source"
    links = [{"text": "Source", "href": "https://b.example/"}]
    got = _node(f"m.linkSegments({json.dumps(text)}, {json.dumps(links)})")
    # text only: the first occurrence after the previous link is the one linked
    assert got == [{"text": "Source", "href": "https://b.example/"}, {"text": " one: Source"}]
    got = _node(
        "m.linkSegments('a X b Y c', [{text: 'X', href: 'https://x.example/'},"
        " {text: 'Y', href: 'http://y.example/'}])"
    )
    assert got == [
        {"text": "a "},
        {"text": "X", "href": "https://x.example/"},
        {"text": " b "},
        {"text": "Y", "href": "http://y.example/"},
        {"text": " c"},
    ]


@needs_node
def test_link_segments_ignore_unsafe_missing_or_absent_links() -> None:
    got = _node(
        "m.linkSegments('a X b', [{text: 'X', href: 'javascript:alert(1)'},"
        " {text: 'X', href: '//x.example'}, {text: 'nope', href: 'https://n.example/'},"
        " {text: '', href: 'https://e.example/'}, null, {text: 'X'}])"
    )
    assert got == [{"text": "a X b"}]
    assert _node("m.linkSegments('plain', undefined)") == [{"text": "plain"}]
    assert _node("m.linkSegments('', [])") == [{"text": ""}]


@needs_node
def test_linked_nodes_build_anchors_with_dom_properties() -> None:
    expr = """(() => {
      const made = [];
      const doc = { createElement(tag) {
        const el = { tag, set innerHTML(v) { throw new Error('innerHTML used'); } };
        made.push(el); return el; } };
      const hl = s => ['hl:' + s];
      const nodes = m.linkedNodes('see X now', [{text: 'X', href: 'https://x.example/a'}], hl, doc);
      return nodes.map(n => typeof n === 'string' ? n : {tag: n.tag, href: n.href, rel: n.rel,
        target: n.target, text: n.textContent});
    })()"""
    assert _node(expr) == [
        "hl:see ",
        {
            "tag": "a",
            "href": "https://x.example/a",
            "rel": "noopener noreferrer",
            "target": "_blank",
            "text": "X",
        },
        "hl: now",
    ]


@needs_node
def test_linked_nodes_without_links_is_just_highlighted_text() -> None:
    expr = """m.linkedNodes('plain para', [], s => ['hl:' + s],
      { createElement() { throw new Error('no element expected'); } })"""
    assert _node(expr) == ["hl:plain para"]
    expr = """m.linkedNodes('a X', [{text: 'X', href: 'javascript:x'}], s => ['hl:' + s],
      { createElement() { throw new Error('no element expected'); } })"""
    assert _node(expr) == ["hl:a X"]


def test_reader_renders_section_paragraphs_through_linked_nodes() -> None:
    js = (LINKS_JS.parent / "reader.js").read_text(encoding="utf-8")
    assert "import { linkedNodes } from './links.js';" in js
    assert "linkedNodes(p, s.links?.[n], hl)" in js
    links = LINKS_JS.read_text(encoding="utf-8")
    assert "innerHTML" not in links
    assert "a.textContent = " in links
    assert "rel = 'noopener noreferrer'" in links
    assert "target = '_blank'" in links
