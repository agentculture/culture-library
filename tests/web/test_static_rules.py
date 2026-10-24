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
    assert {f"{m}.js" for m in ("app", "home", "reader", "search", "upload", "send", "state")} <= (
        names
    )
    assert HTML and CSS.is_file()


def test_no_inline_event_handlers() -> None:
    for path in JS + HTML:
        text = path.read_text(encoding="utf-8")
        hits = [m.group(0) for m in _HANDLER.finditer(text)]
        assert not hits, (path.name, hits)


def test_no_inline_style_attributes_in_markup() -> None:
    for path in JS + HTML:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"""\bstyle\s*=\s*["'`$]""", text), path.name
        assert "setAttribute('style'" not in text and 'setAttribute("style"' not in text


def test_stylesheet_has_no_physical_left_right() -> None:
    css = _strip_css_comments(CSS.read_text(encoding="utf-8"))
    for pat in _PHYSICAL:
        hits = [css[max(0, m.start() - 40) : m.end() + 10] for m in pat.finditer(css)]
        assert not hits, (pat.pattern, hits)


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
