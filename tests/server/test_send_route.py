from __future__ import annotations

import hashlib

import pytest

pytest.importorskip("fastapi")
from fastapi import APIRouter, Depends, Request, Response  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store  # noqa: E402
from culture_library.core import send as core_send  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402
from culture_library.server import routes_send  # noqa: E402
from culture_library.server.auth import Principal, check, current_principal, get_store  # noqa: E402

TARGETS = (
    '[{"label":"x3-ori","type":"Xteink X3 reader","enabled":true,"device_id":"SECRET"},'
    '{"label":"reTerminal Sticky","enabled":false}]'
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(core_send.TARGETS_ENV, TARGETS)
    monkeypatch.delenv(core_send.LOCAL_URL_ENV, raising=False)
    published = []
    monkeypatch.setattr(routes_send, "publish", lambda data: published.append(data))
    app = server_app.create_app()
    app.state.store = store = Store(tmp_path / "data")
    state = {"p": Principal("kid", frozenset({"write", "read"}), private=True)}
    app.dependency_overrides[current_principal] = lambda: state["p"]
    return app, store, published, state


def _item(store, visibility="public"):
    data = b"hello " + visibility.encode()
    return store.add_item(data, kind="book", title="T", format="txt", visibility=visibility).item


def test_send_ok(env):
    app, store, published, _ = env
    item = _item(store)
    r = TestClient(app).post(f"/api/library/{item.id}/send", json={"target": "x3-ori"})
    assert r.status_code == 202
    assert len(published) == 1
    ev = published[0]
    assert ev["sha256"] == item.sha256
    assert ev["target"] == "x3-ori"
    assert ev["requester"] == "kid"
    assert ev["content_url"].endswith(f"/api/library/{item.id}/file")
    feed = TestClient(app).get("/api/feed").json()["events"]
    assert [e["type"] for e in feed if e["type"] == "send_requested"] == ["send_requested"]


def test_refusals(env):
    app, store, published, _ = env
    item = _item(store)
    c = TestClient(app)
    r = c.post(f"/api/library/{item.id}/send", json={"target": "reTerminal Sticky"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "target_disabled"
    r = c.post(f"/api/library/{item.id}/send", json={"target": "nope"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unknown_target"
    assert c.post("/api/library/999/send", json={"target": "x3-ori"}).status_code == 404
    assert published == []


def test_private_hidden_and_scope(env):
    app, store, published, state = env
    item = _item(store, "private")
    state["p"] = Principal("k", frozenset({"write"}), private=False)
    c = TestClient(app)
    assert c.post(f"/api/library/{item.id}/send", json={"target": "x3-ori"}).status_code == 404
    state["p"] = Principal("k", frozenset({"read"}), private=True)
    assert c.post(f"/api/library/{item.id}/send", json={"target": "x3-ori"}).status_code == 403
    assert published == []


def test_broker_down_503_and_no_feed(env, monkeypatch):
    app, store, _, _ = env
    item = _item(store)

    def boom(data):
        raise core_send.PublishUnavailable("broker down")

    monkeypatch.setattr(routes_send, "publish", boom)
    c = TestClient(app)
    r = c.post(f"/api/library/{item.id}/send", json={"target": "x3-ori"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "publish_unavailable"
    assert not [e for e in c.get("/api/feed").json()["events"] if e["type"] == "send_requested"]


def test_targets_route(env):
    app, *_ = env
    r = TestClient(app).get("/api/send/targets")
    assert r.json()["targets"] == [
        {"label": "x3-ori", "type": "Xteink X3 reader", "enabled": True},
        {"label": "reTerminal Sticky", "type": "", "enabled": False},
    ]
    assert "SECRET" not in r.text


def test_rule_with_read_key_fetches_matching_bytes(env, monkeypatch):
    """Criterion 3: a read-scoped consumer fetches the referenced bytes; sha256 matches.

    Uses a minimal fixture route (the real /api/library routes are another task's file).
    """
    app, store, published, state = env
    item = _item(store)
    TestClient(app).post(f"/api/library/{item.id}/send", json={"target": "x3-ori"})
    ev = published[0]

    r = APIRouter()

    @r.get("/fixture/{item_id}/file")
    def f(item_id: int, request: Request, p: Principal = Depends(current_principal)):
        check(p, "read")
        return Response(get_store(request).read_bytes(item_id))

    app.include_router(r)
    state["p"] = Principal("rule", frozenset({"read"}), private=True)
    resp = TestClient(app).get(f"/fixture/{ev["id"]}/file")
    assert resp.status_code == 200
    body = resp.content
    assert hashlib.sha256(body).hexdigest() == ev["sha256"]
