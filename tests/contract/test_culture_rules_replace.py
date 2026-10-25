"""Contract: the culture-rules multipart encoder drives ``PUT /api/library/{id}/file``."""

from __future__ import annotations

import hashlib

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store, keys  # noqa: E402
from culture_library.core.feed import read_since  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402

from . import culture_rules_wire as wire  # noqa: E402


@pytest.fixture
def server(tmp_path):
    app = server_app.create_app()
    store = app.state.store = Store(tmp_path / "data")
    _key, raw = keys.mint(store, "culture-rules todo", ["write"], kinds=["todo"])
    return store, TestClient(app), raw


def _send(client, raw_key, method, path, data, filename="todo.md", fields=()):
    content_type, body = wire.encode_multipart(
        list(fields), [("file", filename, "text/markdown", data)]
    )
    headers = {"Authorization": "Bearer " + raw_key, "Content-Type": content_type}
    return client.request(method, path, content=body, headers=headers, follow_redirects=False)


def test_put_replaces_todo_with_wire_encoder(server):
    store, client, raw = server
    created = _send(
        client,
        raw,
        "POST",
        "/api/library",
        b"- [ ] one\n",
        fields=[("kind", "todo"), ("title", "T")],
    )
    assert created.status_code == 201, created.text
    item = created.json()["item"]
    new = b"- [x] one\n- [ ] two\n"
    r = _send(client, raw, "PUT", f"/api/library/{item['id']}/file", new)
    assert r.status_code == 200, r.text
    assert not r.is_redirect
    body = r.json()
    assert body["id"] == item["id"]
    assert body["sha256"] == hashlib.sha256(new).hexdigest()
    with store.reader() as conn:
        events = read_since(conn, 0, include_private=True)["events"]
    assert [(e["type"], e["item_id"]) for e in events] == [
        ("created", item["id"]),
        ("updated", item["id"]),
    ]


def test_put_refusals_carry_a_code(server):
    _store, client, raw = server
    made = _send(client, raw, "POST", "/api/library", b"- [ ] a\n", fields=[("kind", "todo")])
    item_id = made.json()["item"]["id"]
    empty = _send(client, raw, "PUT", f"/api/library/{item_id}/file", b"")
    assert empty.status_code == 400
    assert wire._error_code(empty.content) == "empty"
    missing = _send(client, raw, "PUT", "/api/library/999/file", b"x")
    assert missing.status_code == 404
    assert wire._error_code(missing.content) == "not_found"
