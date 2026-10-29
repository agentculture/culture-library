"""Static checks over culture_library/web/static (plan task t15).

- No inline event handlers (``onclick=`` and friends) in any .js or .html file, and no
  ``style=`` attributes in markup the JS builds (the CSP has no ``'unsafe-inline'``).
- The stylesheet uses logical properties: no physical left/right (claim c39).
- Content text gets ``dir="auto"`` and is inserted with ``textContent``; ``innerHTML`` is used
  only for the escaped devague fragment and the module's own constant SVG icons.
- No framework, no build step, no CDN script.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "culture_library" / "web" / "static"
JS = sorted(STATIC.glob("*.js"))
HTML = sorted(STATIC.glob("*.html"))
CSS = STATIC / "styles.css"
CSS_FILES = sorted(STATIC.glob("*.css"))
EVALS_JS = [STATIC / n for n in ("evals.js", "evals-model.js", "hashroute.js")]

_HANDLER = re.compile(r"\bon[a-z]+\s*=", re.I)
_PHYSICAL = [
    re.compile(r"\b(?:margin|padding|border)-(?:left|right)\b"),
    re.compile(r"\bborder-(?:top|bottom)-(?:left|right)-radius\b"),
    re.compile(r"(?<![\w-])(?:left|right)\s*:"),
    re.compile(r"\btext-align\s*:\s*(?:left|right)\b"),
    re.compile(r"\bfloat\s*:\s*(?:left|right)\b"),
    re.compile(r"\bclear\s*:\s*(?:left|right)\b"),
]


def _strip_css_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def test_files_exist() -> None:
    names = {p.name for p in JS}
    want = ("app", "home", "reader", "search", "upload", "send", "state", "evals", "evals-model")
    assert {f"{m}.js" for m in (*want, "hashroute")} <= names
    assert HTML
    assert CSS.is_file()
    assert (STATIC / "evals.css").is_file()


def test_no_inline_event_handlers() -> None:
    for path in JS + HTML:
        text = path.read_text(encoding="utf-8")
        hits = [m.group(0) for m in _HANDLER.finditer(text)]
        assert not hits, (path.name, hits)


def test_no_inline_style_attributes_in_markup() -> None:
    for path in JS + HTML:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"""\bstyle\s*=\s*["'`$]""", text), path.name
        assert "setAttribute('style'" not in text
        assert 'setAttribute("style"' not in text


def test_stylesheet_has_no_physical_left_right() -> None:
    assert CSS in CSS_FILES and len(CSS_FILES) >= 2
    for path in CSS_FILES:
        css = _strip_css_comments(path.read_text(encoding="utf-8"))
        for pat in _PHYSICAL:
            hits = [css[max(0, m.start() - 40) : m.end() + 10] for m in pat.finditer(css)]
            assert not hits, (path.name, pat.pattern, hits)


def test_js_positions_with_logical_properties() -> None:
    """Inline positions set through CSSOM (h()'s style map) are logical too."""
    for path in JS:
        text = path.read_text(encoding="utf-8")
        hits = re.findall(r"""['"](?:left|right|margin-left|margin-right|width)['"]\s*:""", text)
        hits += re.findall(r"""style:\s*\{[^}]*\b(?:left|right)\s*:""", text)
        if path in EVALS_JS:
            assert not hits, (path.name, hits)
            assert not re.search(r"""style:\s*\{[^}]*\bwidth\s*:""", text), path.name


def test_evals_text_goes_through_text_nodes() -> None:
    """Titles, URLs and reasons from the API are text nodes with dir=auto, never markup."""
    for path in EVALS_JS:
        text = path.read_text(encoding="utf-8")
        for bad in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "DOMParser",
            "createContextualFragment",
        ):
            assert bad not in text, (path.name, bad)
    js = (STATIC / "evals.js").read_text(encoding="utf-8")
    assert "h('b', { class: 't', dir: 'auto' }, r.title)" in js  # dumbbell row titles
    assert "h('b', { class: cls, dir: 'auto' }, nameOf(r))" in js  # weakest items and URLs
    assert "h('span', { class: 't', dir: 'auto' }, it.title)" in js  # runs table source
    assert "data-tip" in js and "data-evtog" in js and "ev-src" in js  # tips, Table, Source
    # The pure modules stay pure: no DOM and no imports, so Node can test them.
    for name in ("evals-model.js", "hashroute.js"):
        text = (STATIC / name).read_text(encoding="utf-8")
        assert not re.search(r"\b(?:document|window|localStorage|fetch)\s*[.(]", text), name
        assert not re.search(r"^import\b", text, re.M), name


def test_stylesheet_uses_logical_properties() -> None:
    css = CSS.read_text(encoding="utf-8")
    for prop in ("padding-inline", "margin-inline", "border-inline-start", "inset-inline"):
        assert prop in css, prop
    assert "text-align: start" in css


def test_innerhtml_only_for_trusted_constants_and_the_devague_fragment() -> None:
    allowed = re.compile(r"\.innerHTML\s*=\s*(?:ICONS?\[|SVG\.|svgText|fragment\b)")
    for path in JS:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if ".innerHTML" in line or "insertAdjacentHTML" in line or "outerHTML" in line:
                assert allowed.search(line), f"{path.name}:{n}: {line.strip()}"


def test_no_eval_or_dynamic_code() -> None:
    for path in JS:
        text = path.read_text(encoding="utf-8")
        for bad in ("eval(", "new Function", "document.write", "setTimeout('", 'setTimeout("'):
            assert bad not in text, (path.name, bad)


def test_content_text_carries_dir_auto() -> None:
    joined = "\n".join(p.read_text(encoding="utf-8") for p in JS)
    # titles, cards, rows, snippets and reader paragraphs (claim c39)
    for marker in ("card", "row", "snip", "t", "prose"):
        pat = rf"\{{ class: [^{{}}]*?'(?:[\w ]* )?{marker}(?: [\w ]*)?'[^{{}}]*?, dir: 'auto'"
        assert re.search(pat, joined), marker
    assert joined.count("dir: 'auto'") >= 15


def test_no_framework_no_cdn_script() -> None:
    for path in JS + HTML:
        text = path.read_text(encoding="utf-8")
        for bad in ("cdnjs", "unpkg", "jsdelivr", "jszip", "JSZip", "react", "vue"):
            assert bad not in text, (path.name, bad)
        for m in re.finditer(r"""\bimport\s[^;]*?from\s+['"]([^'"]+)['"]""", text):
            assert m.group(1).startswith("./"), (path.name, m.group(1))
    for path in HTML:
        for src in re.findall(r"<script[^>]*\bsrc=\"([^\"]+)\"", path.read_text()):
            assert src.startswith("/static/"), src


def test_send_menu_shows_type_and_status_like_the_prototype() -> None:
    """Prototype: ``<span class="ms">${t.type}. ${t.note}</span>`` (note Enabled / Not enabled
    yet). The type is optional on a target, so without one only the status shows."""
    js = (STATIC / "send.js").read_text(encoding="utf-8")
    assert "const targetNote = t => " in js
    assert "`${t.type}. ${status}`" in js
    assert "'Enabled' : 'Not enabled yet'" in js
    assert "h('span', { class: 'ms' }, targetNote(t))" in js


def test_evals_module_reads_no_app_export_while_it_is_evaluated() -> None:
    """app.js imports evals.js, so evals.js runs before app.js's constants exist: a top-level
    ``const X = SVG...`` would throw (temporal dead zone) and stop the whole site."""
    js = (STATIC / "evals.js").read_text(encoding="utf-8")
    names = re.search(r"import \{([^}]*)\} from './app.js'", js).group(1)
    imported = [n.strip() for n in names.split(",") if n.strip()]
    lines = js.splitlines()
    for n, line in enumerate(lines):
        m = re.match(r"(?:export )?(?:const|let) \w+ = (.*)", line)
        if not m or "=>" in m.group(1):
            continue
        body = [m.group(1)]
        if m.group(1).rstrip().endswith(("{", "[")):  # a multi-line literal, up to its close
            for nxt in lines[n + 1 :]:
                body.append(nxt)
                if re.match(r"[}\]]", nxt):
                    break
        code = "\n".join(body)
        if "=>" in code:
            continue
        code = re.sub(r"'[^']*'|\"[^\"]*\"|`[^`]*`", "''", code)  # drop string literals
        for name in imported:
            assert not re.search(rf"(?<![\w.]){re.escape(name)}\b", code), (name, line)
