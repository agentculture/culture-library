"""Read routes for repo docs: drafts, removed state, design assets, spec links (plan task t16)."""

from __future__ import annotations

import base64
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402
from culture_library.server.auth import Principal, current_principal  # noqa: E402

REMOTE = "https://github.com/agentculture/devague"
A = "a" * 40
B = "b" * 40
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def app(tmp_path):
    application = server_app.create_app()
    application.state.store = Store(tmp_path / "data")
    application.dependency_overrides[current_principal] = lambda: Principal(
        key_id="t", scopes=frozenset({"read", "write"}), private=True
    )
    return application


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def _push(client, path, commit=A, **kw) -> dict:
    body = dict(remote=REMOTE, path=path, commit=commit)
    if commit != "working":
        body.update(committed_at="2026-10-01T00:00:00Z", default_branch_head=commit)
    body.update(kw)
    r = client.post("/api/repodocs", json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _id(client, path) -> int:
    docs = client.get("/api/repodocs", params={"path": path}).json()["docs"]
    return docs[0]["id"]


def test_detail_has_current_draft_versions_removed_links(client) -> None:
    _push(client, "docs/specs/x.md", content="v1\n")
    _push(client, "docs/specs/x.md", commit="working", content="v2 draft\n")
    d = client.get(f"/api/repodocs/{_id(client, 'docs/specs/x.md')}").json()
    assert d["doc"]["path"] == "docs/specs/x.md"
    assert len(d["versions"]) == 1
    assert d["current"]["commit"] == A
    assert d["draft"]["ahead"] is True
    assert d["draft"]["size"] == len("v2 draft\n")
    assert d["removed"] == {"removed": False, "removed_at": None, "removed_commit": None}
    assert d["spec_links"]["code_paths"] == []
    assert d["spec_links"]["pr"] is None


def test_detail_without_draft_and_removed_state(client) -> None:
    _push(client, "docs/specs/y.md", content="v1\n")
    doc = _id(client, "docs/specs/y.md")
    assert client.get(f"/api/repodocs/{doc}").json()["draft"] is None
    r = client.post(
        "/api/repodocs",
        json={"remote": REMOTE, "path": "docs/specs/y.md", "commit": B, "tombstone": True},
    )
    assert r.status_code == 200, r.text
    d = client.get(f"/api/repodocs/{doc}").json()
    assert d["removed"]["removed"] is True
    assert d["removed"]["removed_commit"] == B
    assert len(d["versions"]) == 1


def test_spec_links_in_detail(client) -> None:
    _push(
        client,
        "docs/specs/z.md",
        content="s\n",
        spec_links={"pr": {"url": "https://x/pr/1", "number": 1}, "merge_commit": B},
    )
    d = client.get(f"/api/repodocs/{_id(client, 'docs/specs/z.md')}").json()
    assert d["spec_links"]["pr"]["number"] == 1
    assert d["spec_links"]["merge_commit"] == B


def test_content_version_working_serves_draft(client) -> None:
    _push(client, "docs/specs/x.md", content="v1\n")
    _push(client, "docs/specs/x.md", commit="working", content="draft\n")
    doc = _id(client, "docs/specs/x.md")
    assert client.get(f"/api/repodocs/{doc}/content").text == "v1\n"
    assert (
        client.get(f"/api/repodocs/{doc}/content", params={"version": "working"}).text == "draft\n"
    )
    assert (
        client.get(f"/api/repodocs/{doc}/content", params={"commit": "working"}).text == "draft\n"
    )
    assert client.get(f"/api/repodocs/{doc}/content", params={"version": A}).text == "v1\n"
    r = client.get(f"/api/repodocs/{doc}/content", params={"version": "nothex"})
    assert r.status_code == 400


def test_html_design_is_sandboxed_download(client) -> None:
    html = "<html><script>alert(1)</script></html>"
    _push(client, "design/proto.html", content=html)
    r = client.get(f"/api/repodocs/{_id(client, 'design/proto.html')}/content")
    assert r.status_code == 200
    assert r.text == html
    assert r.headers["content-security-policy"] == "sandbox"
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-content-type-options"] == "nosniff"


def test_svg_is_also_sandboxed(client) -> None:
    _push(client, "design/a.svg", content="<svg onload='x()'/>")
    r = client.get(f"/api/repodocs/{_id(client, 'design/a.svg')}/content")
    assert r.headers["content-security-policy"] == "sandbox"
    assert r.headers["content-disposition"].startswith("attachment")


def test_image_and_pdf_content_types(client) -> None:
    b64 = base64.b64encode(PNG).decode()
    _push(client, "design/shot.png", content_b64=b64)
    _push(client, "design/doc.pdf", content_b64=base64.b64encode(b"%PDF-1.4 x").decode())
    r = client.get(f"/api/repodocs/{_id(client, 'design/shot.png')}/content")
    assert r.headers["content-type"] == "image/png"
    assert r.content == PNG
    assert r.headers["x-content-type-options"] == "nosniff"
    assert (
        "content-security-policy" not in r.headers
        or "sandbox" in r.headers["content-security-policy"]
    )
    r = client.get(f"/api/repodocs/{_id(client, 'design/doc.pdf')}/content")
    assert r.headers["content-type"] == "application/pdf"


def test_markdown_and_json_stay_text_plain(client) -> None:
    _push(client, "docs/specs/x.md", content="# hi\n")
    r = client.get(f"/api/repodocs/{_id(client, 'docs/specs/x.md')}/content")
    assert r.headers["content-type"].startswith("text/plain")
    assert "content-disposition" not in r.headers


def test_rendered_defaults_to_draft_when_ahead(client) -> None:
    _push(client, "docs/specs/x.md", content="# One\n")
    _push(client, "docs/specs/x.md", commit="working", content="# Two\n")
    doc = _id(client, "docs/specs/x.md")
    r = client.get(f"/api/repodocs/{doc}/rendered").json()
    assert r["shown"] == "working"
    assert r["text"] == "# Two\n"
    r = client.get(f"/api/repodocs/{doc}/rendered", params={"version": A}).json()
    assert r["shown"] == A
    assert r["text"] == "# One\n"
    r = client.get(f"/api/repodocs/{doc}/rendered", params={"commit": A}).json()
    assert r["shown"] == A
    r = client.get(f"/api/repodocs/{doc}/rendered", params={"version": "working"}).json()
    assert r["shown"] == "working"


def test_rendered_without_draft_shows_current(client) -> None:
    _push(client, "docs/specs/x.md", content="# One\n")
    r = client.get(f"/api/repodocs/{_id(client, 'docs/specs/x.md')}/rendered").json()
    assert "shown" not in r
    assert r["format"] == "text"


def test_rendered_draft_equal_to_current_is_current(client) -> None:
    _push(client, "docs/specs/x.md", content="# One\n")
    _push(client, "docs/specs/x.md", commit="working", content="# One\n")
    r = client.get(f"/api/repodocs/{_id(client, 'docs/specs/x.md')}/rendered").json()
    assert r["shown"] == A


def test_rendered_reviews_and_questions_markdown(client) -> None:
    for p in (".devague/reviews/r1.md", ".devague/questions/q1.md"):
        _push(client, p, content="# Review\n\n- point\n")
        r = client.get(f"/api/repodocs/{_id(client, p)}/rendered").json()
        assert r["format"] == "markdown"
        assert r["text"].startswith("# Review")
        assert r["title"] == "Review"


def test_rendered_delivery_json_html(client) -> None:
    rec = {"schema_version": 1, "deviations": [], "evidence": [], "deltas": []}
    _push(client, ".devague/deliveries/d.json", content=json.dumps(rec))
    r = client.get(f"/api/repodocs/{_id(client, '.devague/deliveries/d.json')}/rendered").json()
    assert r["format"] == "html"
    assert "shown" not in r
