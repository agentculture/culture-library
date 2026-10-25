"""The site shell: ``GET /`` and ``/static/...`` with security headers (plan task t15)."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.server import app as server_app  # noqa: E402

STATIC = Path(__file__).resolve().parents[2] / "culture_library" / "web" / "static"
PROTOTYPE = Path(__file__).resolve().parents[2] / "docs" / "design" / "prototype.html"
MODULES = ("app", "home", "reader", "search", "upload", "send", "state", "checklist")
CSP_PARTS = (
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "font-src 'self'",
    "img-src 'self' data:",
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
)


@pytest.fixture
def client() -> TestClient:
    # No credentials at all: the shell is public, the data behind it is not.
    return TestClient(server_app.create_app())


def _csp(r) -> list[str]:
    return [p.strip() for p in r.headers["content-security-policy"].split(";") if p.strip()]


def _assert_security_headers(r) -> None:
    parts = _csp(r)
    for want in CSP_PARTS:
        assert want in parts, (want, parts)
    assert not any("unsafe-inline" in p or "unsafe-eval" in p for p in parts)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-frame-options"] == "DENY"


class _Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[tuple[dict, str]] = []
        self.styles: list[str] = []
        self.style_attrs: list[str] = []
        self.handlers: list[str] = []
        self._in: str | None = None
        self._buf = ""
        self._attrs: dict = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.handlers += [k for k in a if k.startswith("on")]
        if "style" in a:
            self.style_attrs.append(tag)
        if tag in ("script", "style"):
            self._in, self._buf, self._attrs = tag, "", a

    def handle_data(self, data):
        if self._in:
            self._buf += data

    def handle_endtag(self, tag):
        if tag == self._in:
            if tag == "script":
                self.scripts.append((self._attrs, self._buf))
            else:
                self.styles.append(self._buf)
            self._in = None


def test_index_served_with_csp_and_no_inline_script(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    _assert_security_headers(r)
    p = _Scripts()
    p.feed(r.text)
    assert p.scripts, "the page loads its modules"
    for attrs, body in p.scripts:
        assert attrs.get("src", "").startswith("/static/"), attrs
        assert not body.strip(), "no inline script"
    assert p.styles == [], "no inline <style>: the stylesheet is a file"
    assert p.style_attrs == [], "no style= attributes (CSP style-src has no unsafe-inline)"
    assert p.handlers == [], "no inline event handlers"
    assert '<script type="module" src="/static/app.js">' in r.text
    assert 'href="/static/styles.css"' in r.text
    assert "fonts.googleapis.com" not in r.text
    assert "fonts.gstatic.com" not in r.text


def test_index_keeps_the_prototype_markup(client: TestClient) -> None:
    html = client.get("/").text
    proto = PROTOTYPE.read_text(encoding="utf-8")
    proto_ids = set(re.findall(r'\bid="([\w-]+)"', proto.split("<script")[0]))
    ids = set(re.findall(r'\bid="([\w-]+)"', html))
    assert proto_ids <= ids, sorted(proto_ids - ids)
    for cls in ("top", "brand", "tabs", "search", "actions", "intro", "shelf", "split", "pane"):
        assert f'class="{cls}' in html or f'class="wrap {cls}' in html, cls
    assert "<title>Culture Library</title>" in html
    assert "Everything you read, record and plan, on one shelf" in html


def test_static_modules_and_stylesheet_types(client: TestClient) -> None:
    for name in MODULES:
        r = client.get(f"/static/{name}.js")
        assert r.status_code == 200, name
        assert r.headers["content-type"].startswith("text/javascript"), name
        _assert_security_headers(r)
    css = client.get("/static/styles.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert "--accent: #00684a" in css.text
    assert "--accent: #5fd897" in css.text


def test_lato_is_self_hosted(client: TestClient) -> None:
    css = client.get("/static/styles.css").text
    assert "googleapis" not in css
    assert "gstatic" not in css
    for weight in (300, 400, 700, 900):
        for subset in ("latin", "latin-ext"):
            name = f"lato-{weight}-{subset}.woff2"
            assert f"/static/fonts/{name}" in css, name
            r = client.get(f"/static/fonts/{name}")
            assert r.status_code == 200, name
            assert r.headers["content-type"] == "font/woff2"
            assert r.content[:4] == b"wOF2"
            _assert_security_headers(r)
    assert client.get("/static/fonts/OFL.txt").status_code == 200
    assert client.get("/static/fonts/nope.woff2").status_code == 404
    assert client.get("/static/fonts/..%2fapp.js").status_code == 404
    assert client.get("/static/fonts/%2e%2e%2fapp.js").status_code == 404
    assert client.get("/static/fonts/a/b.woff2").status_code == 404


def test_static_refuses_traversal_and_unknown_files(client: TestClient) -> None:
    assert client.get("/static/nope.js").status_code == 404
    assert client.get("/static/../server/app.py").status_code == 404
    assert client.get("/static/%2e%2e/server/app.py").status_code == 404
    assert client.get("/static/..%2fserver%2fapp.py").status_code == 404


def test_api_errors_are_not_masked_by_the_site(client: TestClient) -> None:
    # The site never answers for /api: an unauthenticated API call is still a 401.
    assert client.get("/api/library").status_code == 401
    assert client.get("/api/health").status_code == 200


def test_every_module_file_exists_on_disk() -> None:
    for name in MODULES:
        assert (STATIC / f"{name}.js").is_file(), name
    assert (STATIC / "styles.css").is_file()
    assert (STATIC / "index.html").is_file()
