"""``/api/library`` (plan task t10): xteink-compatible upload/list/get/file/delete, plus
visibility, archive/restore and the 30-day purge (decision c45).

Shapes are cited from agentculture/xteink @ ea2f643 (branch feat/issue-1),
``xteink/server/routes_library.py`` and ``docs/api.md``.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store, library, mirror  # noqa: E402
from culture_library.core import search as core_search  # noqa: E402
from culture_library.core.feed import read_since  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402
from culture_library.server import routes_library  # noqa: E402
from culture_library.server.auth import Principal, current_principal  # noqa: E402

READER = Principal("r", frozenset({"read"}))
WRITER = Principal("w", frozenset({"read", "write"}))
OWNER = Principal("o", frozenset({"read", "write"}), private=True)


def make_epub(text: str = "Hello world") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("OEBPS/c1.xhtml", f"<html><body><p>{text}</p></body></html>")
    return buf.getvalue()


class Api:
    """A TestClient whose caller can be switched between principals."""

    def __init__(self, tmp_path, principal: Principal = OWNER) -> None:
        self.app = server_app.create_app()
        self.store = self.app.state.store = Store(tmp_path / "data")
        self.as_(principal)
        self.c = TestClient(self.app)

    def as_(self, principal: Principal) -> Api:
        self.app.dependency_overrides[current_principal] = lambda: principal
        return self

    def upload(self, data: bytes, filename: str = "a.md", **fields):
        return self.c.post(
            "/api/library", files={"file": (filename, data)}, data={k: v for k, v in fields.items()}
        )


@pytest.fixture
def api(tmp_path):
    return Api(tmp_path)


def _events(store, include_private=True):
    with store.reader() as conn:
        return read_since(conn, 0, include_private=include_private)["events"]


# ------------------------------------------------------------------ upload


def test_upload_created_then_deduplicated(api):
    r = api.upload(b"# Dune\n\nspice", "dune.md", title="Dune", author="Herbert", kind="book")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] is True
    item = body["item"]
    for field in ("id", "sha256", "kind", "title", "author", "format", "size", "created_at"):
        assert field in item
    assert item["title"] == "Dune"
    assert item["author"] == "Herbert"
    assert item["kind"] == "book"
    assert item["format"] == "md"
    assert item["size"] == len(b"# Dune\n\nspice")
    assert item["visibility"] == "private"
    assert item["archived_at"] is None

    again = api.upload(b"# Dune\n\nspice", "other.md", title="Other")
    assert again.status_code == 200
    assert again.json() == {"item": item, "created": False}


def test_upload_writes_index_and_created_event_once(api):
    item = api.upload(b"spice melange", "x.txt", title="Arrakis notes").json()["item"]
    api.upload(b"spice melange", "x.txt", title="Arrakis notes")
    events = _events(api.store)
    assert [(e["type"], e["item_id"]) for e in events] == [("created", item["id"])]
    assert events[0]["sha256"] == item["sha256"]
    assert events[0]["visibility"] == "private"
    hits = core_search.fulltext(api.store, "Arrakis", include_private=True)
    assert [h.item_id for h in hits] == [item["id"]]
    with api.store.reader() as conn:
        queued = conn.execute("SELECT item_id FROM index_queue").fetchall()
    assert [r[0] for r in queued] == [item["id"]]


def test_upload_without_visibility_is_private_and_public_is_honoured(api):
    assert api.upload(b"a", "a.txt").json()["item"]["visibility"] == "private"
    assert api.upload(b"b", "b.txt", visibility="public").json()["item"]["visibility"] == "public"


def test_upload_default_title_and_kind(api):
    item = api.upload(make_epub(), "my-book.epub").json()["item"]
    assert item["title"] == "my-book"
    assert item["kind"] == "book"
    assert item["format"] == "epub"
    doc = api.upload(b"notes", "notes.txt").json()["item"]
    assert doc["kind"] == "document"


def test_xteink_article_kind_still_works(api):
    r = api.upload(make_epub("news"), "daily.epub", title="Daily", kind="article")
    assert r.status_code == 201
    assert r.json()["item"]["kind"] == "article"


@pytest.mark.parametrize(
    ("data", "filename", "status", "code"),
    [
        (b"ID3\x03audio", "song.mp3", 415, "unsupported_format"),
        (b"MZ\x90", "tool.exe", 415, "unsupported_format"),
        (b"", "empty.txt", 400, "empty"),
        (b"not a zip", "bad.epub", 400, "bad_magic"),
        (b"{nope", "x.json", 400, "invalid_json"),
    ],
)
def test_upload_refusals_are_detail_code(api, data, filename, status, code):
    r = api.upload(data, filename)
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == {"detail", "code"}
    assert body["code"] == code
    assert isinstance(body["detail"], str)
    assert body["detail"]
    assert _events(api.store) == []


def test_upload_too_large_is_413(api, monkeypatch):
    monkeypatch.setattr(
        routes_library, "UPLOAD_LIMITS", library.IngestLimits(max_bytes=8), raising=True
    )
    r = api.upload(b"0123456789", "big.txt")
    assert r.status_code == 413
    assert r.json()["code"] == "too_large"


def test_upload_bad_kind_and_visibility_are_400(api):
    r = api.upload(b"x", "x.txt", kind="podcast")
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_kind"
    r = api.upload(b"x", "x.txt", visibility="secret")
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_visibility"


def test_upload_missing_file_is_flat_error(api):
    r = api.c.post("/api/library", data={"title": "x"})
    assert r.status_code == 422
    assert set(r.json()) == {"detail", "code"}
    assert r.json()["code"] == "invalid_request"


def test_upload_needs_write_scope_and_kind(api):
    api.as_(READER)
    r = api.upload(b"x", "x.txt")
    assert r.status_code == 403
    assert r.json() == {
        "detail": "needs the write scope",
        "code": "forbidden",
    }
    api.as_(Principal("k", frozenset({"write"}), kinds=frozenset({"article"})))
    assert api.upload(b"x", "x.txt", kind="book").json()["code"] == "forbidden_kind"
    assert api.upload(b"x", "x.txt", kind="article").status_code == 201


def test_unauthenticated_errors_are_flat(tmp_path):
    app = server_app.create_app()
    app.state.store = Store(tmp_path / "data")
    c = TestClient(app)
    r = c.get("/api/library")
    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized"
    assert r.headers["www-authenticate"] == "Bearer"
    r = c.post("/api/library", headers={"Authorization": "Bearer nope"}, files={"file": ("a", b"")})
    assert r.status_code == 401
    assert set(r.json()) == {"detail", "code"}


# ------------------------------------------------------------------ list / get / file


def _seed(api):
    api.as_(OWNER)
    pub = api.upload(
        b"dune spice",
        "d.txt",
        title="Dune",
        author="Frank Herbert",
        kind="book",
        visibility="public",
    ).json()["item"]
    priv = api.upload(b"secret diary", "s.txt", title="Dune diary", kind="diary").json()["item"]
    art = api.upload(b"news", "n.txt", title="Paper", kind="article", visibility="public").json()[
        "item"
    ]
    return pub, priv, art


def test_list_filters_and_pages(api):
    pub, priv, art = _seed(api)
    ids = lambda r: [i["id"] for i in r.json()["items"]]  # noqa: E731
    assert ids(api.c.get("/api/library")) == [pub["id"], priv["id"], art["id"]]
    assert ids(api.c.get("/api/library?kind=article")) == [art["id"]]
    assert ids(api.c.get("/api/library?q=dune")) == [pub["id"], priv["id"]]
    assert ids(api.c.get("/api/library?q=herbert")) == [pub["id"]]
    assert ids(api.c.get("/api/library?limit=1&offset=1")) == [priv["id"]]
    assert ids(api.c.get("/api/library?q=%25")) == []  # LIKE wildcards are literal
    assert api.c.get("/api/library?kind=podcast").json()["code"] == "invalid_kind"
    assert api.c.get("/api/library?limit=0").status_code == 422


def test_private_item_hidden_from_callers_without_private(api):
    pub, priv, _ = _seed(api)
    api.as_(READER)
    listed = [i["id"] for i in api.c.get("/api/library").json()["items"]]
    assert priv["id"] not in listed
    assert pub["id"] in listed
    assert [i["id"] for i in api.c.get("/api/library?q=diary").json()["items"]] == []
    assert core_search.fulltext(api.store, "diary", include_private=False) == []
    for path in (f"/api/library/{priv['id']}", f"/api/library/{priv['id']}/file"):
        r = api.c.get(path)
        assert r.status_code == 404
        assert r.json()["code"] == "not_found"
    # GET adds uploaded_by_you (d4) to the Item fields
    assert api.c.get(f"/api/library/{pub['id']}").json() == {**pub, "uploaded_by_you": False}
    api.as_(OWNER)
    got = api.c.get(f"/api/library/{priv['id']}").json()
    assert {k: v for k, v in got.items() if k != "uploaded_by_you"} == priv


def test_get_missing_is_404(api):
    r = api.c.get("/api/library/999")
    assert r.status_code == 404
    assert set(r.json()) == {"detail", "code"}


def test_file_download(api):
    epub = make_epub()
    item = api.upload(epub, "x.epub", title='Weird / "title"').json()["item"]
    r = api.c.get(f"/api/library/{item['id']}/file")
    assert r.status_code == 200
    assert r.content == epub
    assert r.headers["content-type"] == "application/epub+zip"
    disp = r.headers["content-disposition"]
    assert disp.startswith("attachment;")
    assert ".epub" in disp
    assert "/" not in disp.split("=")[-1]
    md = api.upload(b"# hi", "x.md").json()["item"]
    assert (
        api.c.get(f"/api/library/{md['id']}/file")
        .headers["content-type"]
        .startswith("text/markdown")
    )


def test_read_needs_read_scope(api):
    api.as_(Principal("w", frozenset({"write"})))
    assert api.c.get("/api/library").status_code == 403


# ------------------------------------------------------------------ delete = archive


def test_delete_archives_and_hides(api):
    pub, _, _ = _seed(api)
    r = api.c.delete(f"/api/library/{pub['id']}")
    assert r.status_code == 204
    assert pub["id"] not in [i["id"] for i in api.c.get("/api/library").json()["items"]]
    assert core_search.fulltext(api.store, "Dune", include_private=True)[0].item_id != pub["id"]
    assert api.c.get(f"/api/library/{pub['id']}").status_code == 404
    assert api.c.get(f"/api/library/{pub['id']}/file").status_code == 404
    got = api.c.get(f"/api/library/{pub['id']}?include_archived=1").json()
    assert got["archived_at"] is not None
    assert api.c.get(f"/api/library/{pub['id']}/file?include_archived=1").content == b"dune spice"
    archived = [e for e in _events(api.store) if e["type"] == "archived"]
    assert [(e["item_id"], e["visibility"]) for e in archived] == [(pub["id"], "public")]
    assert archived[0]["data"]["purge_after"] > got["archived_at"]
    # bytes stay on disk until the purge
    assert api.store.blob_path(pub["sha256"]).exists()


def test_include_archived_needs_private(api):
    pub, _, _ = _seed(api)
    api.c.delete(f"/api/library/{pub['id']}")
    api.as_(READER)
    assert api.c.get(f"/api/library/{pub['id']}?include_archived=1").status_code == 404


def test_delete_scope_visibility_and_idempotence(api):
    pub, priv, _ = _seed(api)
    api.as_(READER)
    assert api.c.delete(f"/api/library/{pub['id']}").json()["code"] == "forbidden"
    api.as_(WRITER)
    assert api.c.delete(f"/api/library/{priv['id']}").status_code == 404
    assert api.c.delete("/api/library/999").status_code == 404
    assert api.c.delete(f"/api/library/{pub['id']}").status_code == 204
    assert api.c.delete(f"/api/library/{pub['id']}").status_code == 204
    assert len([e for e in _events(api.store) if e["type"] == "archived"]) == 1


def test_restore_within_window(api):
    pub, _, _ = _seed(api)
    api.c.delete(f"/api/library/{pub['id']}")
    r = api.c.post(f"/api/library/{pub['id']}/restore")
    assert r.status_code == 200
    assert r.json() == pub
    assert pub["id"] in [i["id"] for i in api.c.get("/api/library").json()["items"]]
    assert core_search.fulltext(api.store, "Herbert", include_private=True)[0].item_id == pub["id"]
    restored = [e for e in _events(api.store) if e["type"] == "updated"]
    assert restored[-1]["item_id"] == pub["id"]
    assert restored[-1]["data"] == {"restored": True}
    # not archived any more: nothing to restore
    assert api.c.post(f"/api/library/{pub['id']}/restore").status_code == 404


def test_reupload_of_archived_item_restores_it(api):
    pub, _, _ = _seed(api)
    api.c.delete(f"/api/library/{pub['id']}")
    r = api.upload(b"dune spice", "d.txt", title="Dune")
    assert r.status_code == 200
    assert r.json()["created"] is False
    assert r.json()["item"]["archived_at"] is None
    assert api.c.get(f"/api/library/{pub['id']}").status_code == 200


# ------------------------------------------------------------------ purge (core)


T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _add(store, data, **kw):
    item, _created = library.add(store, data, kw.pop("filename", "x.txt"), **kw)
    return item


def test_purge_removes_only_expired_archives(tmp_path):
    store = Store(tmp_path / "d")
    old = _add(store, b"old", title="old")
    young = _add(store, b"young", title="young")
    live = _add(store, b"live", title="live")
    library.archive(store, old.id, include_private=True, now=T0)
    library.archive(store, young.id, include_private=True, now=T0 + timedelta(days=10))
    assert library.purge_expired(store, now=T0 + timedelta(days=29, hours=23)) == []
    assert library.purge_expired(store, now=T0 + timedelta(days=30, seconds=1)) == [old.id]
    assert store.get_item(old.id) is None
    assert not store.blob_path(old.sha256).exists()
    assert store.get_item(young.id) is not None
    assert store.get_item(live.id) is not None
    with store.reader() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM index_queue WHERE item_id = %s", (old.id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT count(*) FROM item_text WHERE item_id = %s", (old.id,)).fetchone()[
                0
            ]
            == 0
        )
    purged = [e for e in _events(store) if e["type"] == "purged"]
    assert [(e["item_id"], e["sha256"]) for e in purged] == [(old.id, old.sha256)]
    # idempotent
    assert library.purge_expired(store, now=T0 + timedelta(days=60)) == [young.id]
    assert library.purge_expired(store, now=T0 + timedelta(days=90)) == []


def test_purge_keeps_blob_shared_with_another_table(tmp_path):
    store = Store(tmp_path / "d")
    item = _add(store, b"shared bytes", title="shared")
    with store.connect() as conn:  # a pending mirror upload of the same bytes
        mirror.enqueue(conn, "x/docs/shared.md@working", item.sha256, "text/markdown")
    library.archive(store, item.id, include_private=True, now=T0)
    assert library.purge_expired(store, now=T0 + timedelta(days=31)) == [item.id]
    assert store.blob_path(item.sha256).exists()


def test_blob_tables_name_every_table_that_stores_a_blob_sha(tmp_path):
    """purge consults library.BLOB_TABLES, a fixed list: every ``sha256`` column in the schema
    names a stored blob except the feed's, which is history (an event keeps no file alive)."""
    store = Store(tmp_path / "d")
    with store.reader() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND column_name = 'sha256'"
        ).fetchall()
    assert {r[0] for r in rows} - {"feed"} == set(library.BLOB_TABLES)


def test_restore_refused_after_window(tmp_path):
    store = Store(tmp_path / "d")
    item = _add(store, b"x", title="x")
    library.archive(store, item.id, include_private=True, now=T0)
    too_late = T0 + timedelta(days=31)
    with pytest.raises(library.NotFoundError):
        library.restore(store, item.id, include_private=True, now=too_late)
    back = library.restore(store, item.id, include_private=True, now=T0 + timedelta(days=29))
    assert back.archived_at is None


def test_add_heals_missing_created_event_and_blob(tmp_path):
    store = Store(tmp_path / "d")
    # an item stored by a path that skipped the feed/index (e.g. a crash, or Store.add_item)
    result = store.add_item(b"orphan", kind="document", title="orphan", format="txt")
    store.blob_path(result.item.sha256).unlink()
    item, created = library.add(store, b"orphan", "o.txt")
    assert created is False
    assert item.id == result.item.id
    assert [e["type"] for e in _events(store)] == ["created"]
    assert store.blob_path(item.sha256).read_bytes() == b"orphan"


def test_purger_thread_runs_and_stops(tmp_path):
    store = Store(tmp_path / "d")
    item = _add(store, b"x", title="x")
    library.archive(
        store, item.id, include_private=True, now=datetime.now(UTC) - timedelta(days=40)
    )
    purger = library.Purger(store, interval_s=0.01)
    purger.start()
    try:
        assert purger.ran.wait(5)
    finally:
        purger.stop()
    assert store.get_item(item.id) is None
    assert not purger.is_alive()


def test_router_lifespan_starts_purger(tmp_path, monkeypatch):
    monkeypatch.setenv(library.PURGE_INTERVAL_ENV, "3600")
    app = server_app.create_app()
    app.state.store = Store(tmp_path / "data")
    with TestClient(app):
        purger = app.state.library_purger
        assert purger.is_alive()
    assert not purger.is_alive()
    monkeypatch.setenv(library.PURGE_INTERVAL_ENV, "0")
    app = server_app.create_app()
    app.state.store = Store(tmp_path / "data2")
    with TestClient(app):
        assert getattr(app.state, "library_purger", None) is None


# ------------------------------------------------------------------ dedup vs visibility


def test_dedup_against_private_item_is_opaque_conflict_for_non_private(api):
    priv = api.upload(b"my diary", "d.txt", title="Diary", kind="diary").json()["item"]
    api.as_(WRITER)
    r = api.upload(b"my diary", "copy.txt", title="Copy")
    assert r.status_code == 409
    assert r.json() == {"detail": "an item with these bytes already exists", "code": "conflict"}
    assert str(priv["id"]) not in r.text
    assert priv["sha256"] not in r.text
    assert [e["type"] for e in _events(api.store)] == ["created"]


def test_conflict_never_restores_an_archived_private_item(api):
    priv = api.upload(b"my diary", "d.txt", title="Diary").json()["item"]
    api.c.delete(f"/api/library/{priv['id']}")
    api.as_(WRITER)
    assert api.upload(b"my diary", "d.txt").status_code == 409
    assert api.store.get_item(priv["id"]).archived_at is not None
    assert [e["type"] for e in _events(api.store)] == ["created", "archived"]


def test_private_caller_keeps_dedup_and_restore_on_private_item(api):
    priv = api.upload(b"my diary", "d.txt", title="Diary").json()["item"]
    api.c.delete(f"/api/library/{priv['id']}")
    r = api.upload(b"my diary", "d.txt")
    assert r.status_code == 200
    assert r.json()["created"] is False
    assert r.json()["item"]["id"] == priv["id"]
    assert r.json()["item"]["archived_at"] is None


def test_public_existing_item_dedups_for_everyone(api):
    pub = api.upload(b"open", "o.txt", title="Open", visibility="public").json()["item"]
    for who in (WRITER, OWNER):
        r = api.as_(who).upload(b"open", "o.txt")
        assert r.status_code == 200
        assert r.json() == {"item": pub, "created": False}


# ------------------------------------------------------------------ body cap


def test_declared_oversize_body_is_413_before_reading(api, monkeypatch):
    monkeypatch.setattr(routes_library, "_FORM_SLACK", 0)
    monkeypatch.setattr(routes_library, "UPLOAD_LIMITS", library.IngestLimits(max_bytes=100))
    r = api.upload(b"x" * 500, "big.txt")
    assert r.status_code == 413
    assert r.json() == {
        "detail": "file exceeds the size limit",
        "code": "too_large",
    }


def test_chunked_oversize_body_is_413_while_streaming(api, monkeypatch):
    monkeypatch.setattr(routes_library, "_FORM_SLACK", 0)
    monkeypatch.setattr(routes_library, "UPLOAD_LIMITS", library.IngestLimits(max_bytes=100))
    sent = []

    def body():
        yield b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\n'
        for _ in range(50):
            sent.append(1)
            yield b"y" * 64
        yield b"\r\n--b--\r\n"

    r = api.c.post(
        "/api/library", content=body(), headers={"Content-Type": "multipart/form-data; boundary=b"}
    )
    assert r.status_code == 413
    assert r.json()["code"] == "too_large"
    assert "content-length" not in {k.lower() for k in r.request.headers}


def test_non_multipart_body_is_invalid_request(api):
    r = api.c.post("/api/library", json={"file": "x"})
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_request"


# ------------------------------------------------------------------ indexer wake


class _FakeIndexer:
    def __init__(self, store):
        self.store = store
        self.woken = 0

    def notify(self):
        self.woken += 1


def test_upload_wakes_the_running_indexer_for_its_store(api, monkeypatch):
    import types

    mine, other = _FakeIndexer(api.store), _FakeIndexer(object())
    owner = types.ModuleType("culture_library.server.routes_search")
    owner._indexers = [mine, other]
    monkeypatch.setitem(__import__("sys").modules, owner.__name__, owner)
    api.upload(b"wake", "w.txt")
    assert (mine.woken, other.woken) == (1, 0)
    api.upload(b"bad", "w.exe")
    assert mine.woken == 1  # refused uploads do not wake it

    calls = []
    owner.notify_indexer = calls.append
    api.upload(b"wake2", "w.txt")
    assert calls == [api.store]
    assert mine.woken == 1


def test_upload_without_search_module_still_works(api, monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "culture_library.server.routes_search", None)
    assert api.upload(b"lonely", "l.txt").status_code == 201


def test_uploader_key_gets_its_own_private_item_back_but_others_conflict(api):
    pusher = Principal("pusher", frozenset({"write"}))
    first = api.as_(pusher).upload(b"newspaper", "n.epub.txt", title="Paper").json()
    assert first["created"] is True
    assert first["item"]["visibility"] == "private"
    again = api.upload(b"newspaper", "n.epub.txt")
    assert again.status_code == 200
    assert again.json() == {"item": first["item"], "created": False}
    assert (
        api.as_(Principal("other", frozenset({"write"}))).upload(b"newspaper", "n.txt").status_code
        == 409
    )
    # archived: even the uploader cannot restore it without private access
    api.as_(OWNER).c.delete(f"/api/library/{first['item']['id']}")
    assert api.as_(pusher).upload(b"newspaper", "n.txt").status_code == 409
    assert api.store.get_item(first["item"]["id"]).archived_at is not None


def test_add_retries_when_item_vanishes_before_the_follow_up(tmp_path, monkeypatch):
    store = Store(tmp_path / "d")
    item = _add(store, b"gone", title="gone")
    real_row = library._row
    seen = []

    def flaky_row(conn, item_id):  # first follow-up lookup sees it purged
        seen.append(item_id)
        return None if len(seen) == 1 else real_row(conn, item_id)

    monkeypatch.setattr(library, "_row", flaky_row)
    again, created = library.add(store, b"gone", "g.txt")
    assert seen == [item.id, item.id]  # retried once, outside the first transaction
    assert created is False
    assert again.id == item.id


@pytest.mark.parametrize("how", ["crc", "method", "encrypted", "deflate", "mimetype_crc"])
def test_upload_corrupt_epub_is_a_400_refusal(api, how):
    from culture_library.core.ingest import read_refusals
    from tests.core.test_ingest import corrupt_epub

    r = api.upload(corrupt_epub(how), "bad.epub")
    assert r.status_code == 400, r.text
    assert r.json() == {"detail": r.json()["detail"], "code": "corrupt_archive"}
    assert read_refusals(api.store)[-1]["code"] == "corrupt_archive"


# ------------------------------------------------------------------ dedup restore vs kind limits


@pytest.mark.parametrize("private", [True, False])
def test_dedup_never_restores_an_archived_item_of_a_kind_the_key_cannot_write(api, private):
    book = api.upload(b"moby", "m.txt", title="Moby", kind="book", visibility="public")
    book = book.json()["item"]
    api.c.delete(f"/api/library/{book['id']}")
    diary_only = Principal("d", frozenset({"read", "write"}), frozenset({"diary"}), private)
    r = api.as_(diary_only).upload(b"moby", "m.txt", kind="diary")
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "this key cannot write kind book", "code": "forbidden_kind"}
    assert api.store.get_item(book["id"]).archived_at is not None
    assert [e["type"] for e in _events(api.store)] == ["created", "archived"]
    # same rule as the restore route
    assert api.c.post(f"/api/library/{book['id']}/restore").json()["code"] == "forbidden_kind"


def test_dedup_restores_for_a_key_that_may_write_the_existing_kind(api):
    book = api.upload(b"moby", "m.txt", title="Moby", kind="book").json()["item"]
    api.c.delete(f"/api/library/{book['id']}")
    books = Principal("b", frozenset({"read", "write"}), frozenset({"book"}), True)
    r = api.as_(books).upload(b"moby", "m.txt", kind="book")
    assert r.status_code == 200
    assert r.json()["item"]["archived_at"] is None


def test_dedup_of_a_live_item_of_another_kind_is_returned_untouched(api):
    book = api.upload(b"moby", "m.txt", title="Moby", kind="book", visibility="public")
    diary_only = Principal("d", frozenset({"read", "write"}), frozenset({"diary"}))
    r = api.as_(diary_only).upload(b"moby", "m.txt", kind="diary")
    assert r.status_code == 200
    assert r.json() == {"item": book.json()["item"], "created": False}


def test_dedup_private_item_of_another_owner_stays_opaque_409_for_kind_limited_key(api):
    priv = api.upload(b"secret", "s.txt", title="S", kind="book").json()["item"]
    api.c.delete(f"/api/library/{priv['id']}")
    diary_only = Principal("d", frozenset({"read", "write"}), frozenset({"diary"}))
    r = api.as_(diary_only).upload(b"secret", "s.txt", kind="diary")
    assert r.status_code == 409
    assert r.json()["code"] == "conflict"


# ------------------------------------------------------------------ upload indexes the body once


def test_upload_indexes_the_extracted_body_so_the_indexer_only_embeds(api, monkeypatch):
    from culture_library.core import indexer as core_indexer
    from culture_library.core import ingest as core_ingest

    calls = []
    real = core_ingest.extract_text_ex

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(core_ingest, "extract_text_ex", counting)
    item = api.upload(b"# Memo\n\nthe **okapi** grazes", "m.md").json()["item"]
    assert len(calls) == 1
    with api.store.reader() as conn:
        body = conn.execute(
            "SELECT body FROM item_text WHERE item_id = %s", (item["id"],)
        ).fetchone()
        done = conn.execute(
            "SELECT body_done FROM index_queue WHERE item_id = %s", (item["id"],)
        ).fetchone()
    assert body[0] == real(b"# Memo\n\nthe **okapi** grazes", "md").text
    assert done[0] == 1
    assert [h.item_id for h in core_search.fulltext(api.store, "okapi", include_private=True)] == [
        item["id"]
    ]

    def never(_item):
        raise AssertionError("the indexer extracted the body again")

    embedded = []

    def embed(texts):
        embedded.extend(texts)
        return [[1.0] + [0.0] * 767 for _ in texts]  # the current strategy's 768 dimensions

    ix = core_indexer.Indexer(api.store, embed, text_for=never)
    assert ix.run_once() == 1
    assert any("okapi" in t for t in embedded)
    assert len(calls) == 1
