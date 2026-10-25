"""``/api/library/{id}/sources``: the newspaper sources sidecar."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store, keys  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402

MANIFEST = [
    {
        "article_title": "Port reopens",
        "source_urls": ["https://example.org/a", "http://example.org/b"],
        "excerpt": "The port reopened on Monday.",
    }
]


@pytest.fixture
def env(tmp_path):
    app = server_app.create_app()
    store = app.state.store = Store(tmp_path / "data")
    app.state.embedder = None
    with TestClient(app) as http:

        def key(name, scopes, **kw):
            return {"Authorization": f"Bearer {keys.mint(store, name, scopes, **kw)[1]}"}

        def item(body, kind="newspaper", visibility="public", title="t"):
            return store.add_item(
                body, kind=kind, title=title, format="txt", visibility=visibility
            ).item

        yield type("Env", (), {"http": http, "key": staticmethod(key), "item": staticmethod(item)})


def test_put_then_get_roundtrip(env):
    it = env.item(b"paper one")
    admin = env.key("w", ["read", "write"])
    r = env.http.put(f"/api/library/{it.id}/sources", json=MANIFEST, headers=admin)
    assert r.status_code == 200, r.text
    got = env.http.get(f"/api/library/{it.id}/sources", headers=admin)
    assert got.status_code == 200
    body = got.json()
    assert body["item_id"] == it.id
    assert body["sources"] == MANIFEST
    assert body["updated_at"]
    again = env.http.put(f"/api/library/{it.id}/sources", json=[], headers=admin)
    assert again.status_code == 200
    assert env.http.get(f"/api/library/{it.id}/sources", headers=admin).json()["sources"] == []


def test_get_without_sidecar_is_no_sources(env):
    it = env.item(b"paper two")
    r = env.http.get(f"/api/library/{it.id}/sources", headers=env.key("r", ["read"]))
    assert r.status_code == 404
    assert r.json()["code"] == "no_sources"


def test_missing_item_is_not_found(env):
    r = env.http.get("/api/library/999/sources", headers=env.key("r", ["read"]))
    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    r = env.http.put("/api/library/999/sources", json=MANIFEST, headers=env.key("w", ["write"]))
    assert (r.status_code, r.json()["code"]) == (404, "not_found")


def test_read_only_key_cannot_put(env):
    it = env.item(b"paper three")
    r = env.http.put(f"/api/library/{it.id}/sources", json=MANIFEST, headers=env.key("r", ["read"]))
    assert r.status_code == 403


def test_unauthenticated_is_401(env):
    it = env.item(b"paper four")
    assert env.http.get(f"/api/library/{it.id}/sources").status_code == 401


def test_private_item_hidden_from_non_owner_public_key(env):
    it = env.item(b"secret paper", visibility="private")
    owner = env.key("o", ["read", "write"], private=True)
    assert (
        env.http.put(f"/api/library/{it.id}/sources", json=MANIFEST, headers=owner).status_code
        == 200
    )
    other = env.key("pub", ["read", "write"])
    r = env.http.get(f"/api/library/{it.id}/sources", headers=other)
    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    r = env.http.put(f"/api/library/{it.id}/sources", json=MANIFEST, headers=other)
    assert r.status_code == 404
    assert env.http.get(f"/api/library/{it.id}/sources", headers=owner).status_code == 200


def test_kind_restricted_key(env):
    paper = env.item(b"a paper", kind="newspaper")
    book = env.item(b"a book", kind="book")
    k = env.key("rules", ["read", "write"], kinds=["newspaper"])
    assert (
        env.http.put(f"/api/library/{paper.id}/sources", json=MANIFEST, headers=k).status_code
        == 200
    )
    r = env.http.put(f"/api/library/{book.id}/sources", json=MANIFEST, headers=k)
    assert (r.status_code, r.json()["code"]) == (403, "forbidden_kind")


def test_no_feed_event(env):
    it = env.item(b"feed paper")
    h = env.key("w", ["read", "write"])
    before = env.http.get("/api/feed", headers=h).json()
    env.http.put(f"/api/library/{it.id}/sources", json=MANIFEST, headers=h)
    assert env.http.get("/api/feed", headers=h).json() == before


@pytest.mark.parametrize(
    "bad",
    [
        {"not": "a list"},
        ["str"],
        [{"article_title": "", "source_urls": ["https://x.org"], "excerpt": ""}],
        [{"article_title": "t", "source_urls": [], "excerpt": ""}],
        [{"article_title": "t", "source_urls": ["ftp://x.org"], "excerpt": ""}],
        [{"article_title": "t", "source_urls": [1], "excerpt": ""}],
        [{"article_title": "t", "source_urls": ["https://x.org"], "excerpt": 5}],
        [{"article_title": "t", "source_urls": ["https://x.org"], "excerpt": "x" * 2001}],
        [{"article_title": "t", "source_urls": ["https://x.org"]}],
        [{"article_title": "t", "source_urls": ["https://"], "excerpt": ""}],
        [MANIFEST[0]] * 501,
    ],
)
def test_invalid_manifest_is_422(env, bad):
    it = env.item(b"validation paper")
    h = env.key("w", ["read", "write"])
    r = env.http.put(f"/api/library/{it.id}/sources", json=bad, headers=h)
    assert r.status_code == 422, r.text
    assert env.http.get(f"/api/library/{it.id}/sources", headers=h).status_code == 404


def test_boundary_values_accepted(env):
    it = env.item(b"boundary paper")
    h = env.key("w", ["read", "write"])
    entry = {"article_title": "t", "source_urls": ["https://x.org"], "excerpt": "x" * 2000}
    r = env.http.put(f"/api/library/{it.id}/sources", json=[entry] * 500, headers=h)
    assert r.status_code == 200, r.text


def test_invalid_json_is_422(env):
    it = env.item(b"json paper")
    h = {**env.key("w", ["read", "write"]), "Content-Type": "application/json"}
    r = env.http.put(f"/api/library/{it.id}/sources", content=b"{nope", headers=h)
    assert r.status_code == 422
