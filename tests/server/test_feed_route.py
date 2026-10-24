from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store  # noqa: E402
from culture_library.core.feed import append  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402
from culture_library.server.auth import Principal, current_principal  # noqa: E402


def _client(tmp_path, principal):
    app = server_app.create_app()
    app.state.store = Store(tmp_path / "data")
    app.dependency_overrides[current_principal] = lambda: principal
    return app.state.store, TestClient(app)


def _seed(store):
    with store.connect() as conn:
        for i, vis in enumerate(["public", "private", "public"], start=1):
            append(
                conn,
                {
                    "type": "created",
                    "item_id": i,
                    "sha256": "b" * 64,
                    "kind": "book",
                    "visibility": vis,
                },
            )


def test_feed_in_order_and_since(tmp_path):
    store, c = _client(tmp_path, Principal("k", frozenset({"read"}), private=True))
    _seed(store)
    body = c.get("/api/feed").json()
    assert [e["item_id"] for e in body["events"]] == [1, 2, 3]
    assert body["cursor"] == body["events"][-1]["cursor"] and body["more"] is False
    since = c.get(f"/api/feed?since={body['events'][0]['cursor']}").json()
    assert [e["item_id"] for e in since["events"]] == [2, 3]


def test_private_hidden_but_cursor_advances(tmp_path):
    store, c = _client(tmp_path, Principal("k", frozenset({"read"})))
    _seed(store)
    tail = c.get("/api/feed?since=1").json()
    assert [e["item_id"] for e in tail["events"]] == [3]
    body = c.get("/api/feed").json()
    assert [e["item_id"] for e in body["events"]] == [1, 3]
    only_private_tail = c.get("/api/feed?since=2&limit=1").json()
    assert only_private_tail["cursor"] == 3


def test_requires_read_scope_and_limit_bounds(tmp_path):
    _, c = _client(tmp_path, Principal("k", frozenset({"write"})))
    assert c.get("/api/feed").status_code == 403
    _, c = _client(tmp_path / "x", Principal("k", frozenset({"read"})))
    assert c.get("/api/feed?limit=1001").status_code == 422
    assert c.get("/api/feed?limit=0").status_code == 422


def test_feed_filtered_to_one_item(tmp_path):
    store, c = _client(tmp_path, Principal("k", frozenset({"read"}), private=True))
    _seed(store)
    _seed(store)
    body = c.get("/api/feed?item_id=2").json()
    assert [e["item_id"] for e in body["events"]] == [2, 2]
    assert c.get("/api/feed?item_id=0").status_code == 422
    store2, pub = _client(tmp_path / "p", Principal("k", frozenset({"read"})))
    _seed(store2)
    assert pub.get("/api/feed?item_id=2").json()["events"] == []
    assert [e["item_id"] for e in pub.get("/api/feed?item_id=3").json()["events"]] == [3]
