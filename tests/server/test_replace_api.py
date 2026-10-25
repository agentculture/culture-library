"""``PUT /api/library/{id}/file``: replace an item's bytes under the same id (issue #10)."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store, library  # noqa: E402
from culture_library.core.feed import read_since  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402
from culture_library.server import routes_library  # noqa: E402
from culture_library.server.auth import Principal, current_principal  # noqa: E402

OWNER = Principal("o", frozenset({"read", "write"}), private=True)
WRITER = Principal("w", frozenset({"read", "write"}))
READER = Principal("r", frozenset({"read"}))
NOTE_WRITER = Principal("n", frozenset({"read", "write"}), kinds=frozenset({"article"}))

OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('book', 'article', 'newspaper', 'document', 'transcript',
        'diary', 'spec', 'plan', 'devague-record')),
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    format TEXT NOT NULL,
    size INTEGER NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'public')),
    created_at TEXT NOT NULL,
    archived_at TEXT
);
"""


class Api:
    def __init__(self, tmp_path, principal: Principal = OWNER) -> None:
        self.app = server_app.create_app()
        self.store = self.app.state.store = Store(tmp_path / "data")
        self.as_(principal)
        self.c = TestClient(self.app)

    def as_(self, principal: Principal) -> Api:
        self.app.dependency_overrides[current_principal] = lambda: principal
        return self

    def upload(self, data: bytes, filename: str = "a.md", **fields):
        return self.c.post("/api/library", files={"file": (filename, data)}, data=fields)

    def put(self, item_id, data: bytes, filename: str = "b.md"):
        return self.c.put(f"/api/library/{item_id}/file", files={"file": (filename, data)})

    def events(self):
        with self.store.reader() as conn:
            return read_since(conn, 0, include_private=True)["events"]


@pytest.fixture
def api(tmp_path):
    return Api(tmp_path)


def _make(api, data=b"# v1\n", **fields):
    fields.setdefault("kind", "document")
    r = api.upload(data, **fields)
    assert r.status_code == 201, r.text
    return r.json()["item"]


# ---------------------------------------------------------------- criterion 1


def test_put_replaces_bytes_under_same_id(api):
    item = _make(api)
    new = b"# v2\n\nnew text\n"
    r = api.put(item["id"], new)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == item["id"]
    assert body["sha256"] == hashlib.sha256(new).hexdigest() != item["sha256"]
    got = api.c.get(f"/api/library/{item['id']}").json()
    assert got["sha256"] == body["sha256"]
    assert api.c.get(f"/api/library/{item['id']}/file").content == new


def test_put_appends_exactly_one_updated_event(api):
    item = _make(api)
    before = len(api.events())
    assert api.put(item["id"], b"# v2\n").status_code == 200
    new = api.events()[before:]
    assert [(e["type"], e["item_id"]) for e in new] == [("updated", item["id"])]
    feed = api.c.get("/api/feed?since=0").json()["events"]
    assert [e["type"] for e in feed if e["item_id"] == item["id"]].count("updated") == 1


def test_put_same_bytes_is_a_noop(api):
    item = _make(api)
    before = len(api.events())
    r = api.put(item["id"], b"# v1\n")
    assert r.status_code == 200
    assert r.json()["sha256"] == item["sha256"]
    assert len(api.events()) == before


# ---------------------------------------------------------------- criterion 2


def test_wrong_kind_key_is_403_and_item_unchanged(api):
    item = _make(api, visibility="public")  # kind=document; NOTE_WRITER may only write article
    r = api.as_(NOTE_WRITER).put(item["id"], b"# hacked\n")
    assert r.status_code == 403
    assert r.json()["code"] == "forbidden_kind"
    api.as_(OWNER)
    assert api.c.get(f"/api/library/{item['id']}").json()["sha256"] == item["sha256"]
    assert api.c.get(f"/api/library/{item['id']}/file").content == b"# v1\n"


def test_read_only_key_is_403(api):
    item = _make(api)
    assert api.as_(READER).put(item["id"], b"# x\n").status_code == 403


def test_private_item_is_404_for_non_private_key(api):
    item = _make(api, visibility="private")
    r = api.as_(WRITER).put(item["id"], b"# x\n")
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


def test_archived_item_is_404(api):
    item = _make(api)
    assert api.c.delete(f"/api/library/{item['id']}").status_code == 204
    assert api.put(item["id"], b"# x\n").status_code == 404


def test_absent_item_is_404(api):
    assert api.put(9999, b"# x\n").status_code == 404


# ---------------------------------------------------------------- criterion 3


def test_todo_upload_on_store_created_with_old_schema(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    conn = sqlite3.connect(data / "library.db", isolation_level=None)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO items (sha256, kind, title, format, size, created_at) "
        "VALUES (?, 'book', 'Old', 'md', 1, '2026-10-01T00:00:00+00:00')",
        ("a" * 64,),
    )
    conn.close()
    app = server_app.create_app()
    app.state.store = Store(data)
    app.dependency_overrides[current_principal] = lambda: OWNER
    client = TestClient(app)
    r = client.post(
        "/api/library", files={"file": ("t.md", b"- [ ] thing\n")}, data={"kind": "todo"}
    )
    assert r.status_code == 201, r.text
    assert r.json()["item"]["kind"] == "todo"


# ---------------------------------------------------------------- criterion 4


def test_empty_is_400(api):
    item = _make(api)
    r = api.put(item["id"], b"")
    assert (r.status_code, r.json()["code"]) == (400, "empty")


def test_bad_magic_is_400(api):
    item = _make(api)
    r = api.put(item["id"], b"not a pdf", "doc.pdf")
    assert (r.status_code, r.json()["code"]) == (400, "bad_magic")


def test_invalid_encoding_is_400(api):
    item = _make(api)
    r = api.put(item["id"], b"\xff\xfe\xfa bad", "c.md")
    assert (r.status_code, r.json()["code"]) == (400, "invalid_encoding")


def test_unsupported_format_is_415(api):
    item = _make(api)
    r = api.put(item["id"], b"data", "c.exe")
    assert (r.status_code, r.json()["code"]) == (415, "unsupported_format")


def test_too_large_is_413_by_core_and_by_body_cap(api, monkeypatch):
    item = _make(api)
    monkeypatch.setattr(routes_library, "UPLOAD_LIMITS", library.IngestLimits(max_bytes=64))
    r = api.put(item["id"], b"x" * 100)  # past the core limit, inside the form slack
    assert (r.status_code, r.json()["code"]) == (413, "too_large")
    monkeypatch.setattr(routes_library, "_FORM_SLACK", 0)
    r = api.put(item["id"], b"x" * 4096)  # past the body cap itself
    assert (r.status_code, r.json()["code"]) == (413, "too_large")
    assert api.c.get(f"/api/library/{item['id']}/file").content == b"# v1\n"


def test_duplicate_of_visible_item_names_it(api):
    a = _make(api)
    b = _make(api, data=b"# other\n", visibility="public")
    r = api.put(a["id"], b"# other\n")
    assert r.status_code == 409
    assert r.json()["code"] == "duplicate_content"
    assert r.json()["other_id"] == b["id"]


def test_duplicate_of_invisible_item_hides_id(api):
    a = _make(api, visibility="public")
    _make(api, data=b"# secret\n", visibility="private")
    r = api.as_(WRITER).put(a["id"], b"# secret\n")
    assert r.status_code == 409
    assert r.json()["code"] == "duplicate_content"
    assert "other_id" not in r.json()
    assert str(a["id"] + 1) not in r.json()["detail"]


# ---------------------------------------------------------------- criterion 5


@pytest.mark.parametrize("method", ["PUT", "DELETE", "MKCOL"])
def test_dav_writes_still_405(api, method):
    r = api.c.request(method, "/dav/", content=b"x")
    assert r.status_code == 405


def test_duplicate_of_archived_item_hides_id_from_non_private_key(api):
    a = _make(api, visibility="public")
    b = _make(api, data=b"# gone\n", visibility="public")
    assert api.c.delete(f"/api/library/{b['id']}").status_code == 204
    assert api.as_(WRITER).c.get(f"/api/library/{b['id']}").status_code == 404
    r = api.as_(WRITER).put(a["id"], b"# gone\n")
    assert r.status_code == 409
    assert r.json()["code"] == "duplicate_content"
    assert "other_id" not in r.json()
