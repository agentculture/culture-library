"""An uploader key reads and edits its own private items (plan deviation d4, item 2).

The rule, applied everywhere visibility is decided: an item is visible if it is public, or
the caller has ``private``, or the caller's key uploaded it. Nothing else widens: a diary key
still cannot write other kinds or see other keys' private items, and identical bytes of
someone else's private item still answer an opaque 409.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET  # nosec B405 - parsing our own test output

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store  # noqa: E402
from culture_library.core import indexer as ix  # noqa: E402
from culture_library.core import send as core_send  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402
from culture_library.server import routes_send  # noqa: E402
from culture_library.server.auth import Principal, current_principal  # noqa: E402

RW = frozenset({"read", "write"})
ALICE = Principal("alice", RW, kinds=frozenset({"diary"}))
BOB = Principal("bob", RW, kinds=frozenset({"diary"}))
OWNER = Principal("owner", RW, private=True)
ATOM = "{http://www.w3.org/2005/Atom}"
DAV = "{DAV:}"


class Api:
    def __init__(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv(core_send.TARGETS_ENV, '[{"label":"x3","enabled":true}]')
        self.published: list = []
        monkeypatch.setattr(routes_send, "publish", self.published.append)
        self.app = server_app.create_app()
        self.store = self.app.state.store = Store(tmp_path / "data")
        self.app.state.embedder = None
        self.c = TestClient(self.app)
        self.as_(OWNER)

    def as_(self, principal: Principal) -> TestClient:
        self.app.dependency_overrides[current_principal] = lambda: principal
        return self.c

    def upload(self, principal, data, filename="e.md", **fields):
        return self.as_(principal).post(
            "/api/library", files={"file": (filename, data)}, data=fields
        )


@pytest.fixture
def api(tmp_path, monkeypatch):
    return Api(tmp_path, monkeypatch)


def _entry(api, who=ALICE, text=b"# Dear diary\n\nthe aardvark sleeps"):
    r = api.upload(who, text, "e.md", kind="diary", title="Dear diary")
    assert r.status_code == 201, r.text
    item = r.json()["item"]
    assert item["visibility"] == "private"
    return item["id"]


def _ids_list(c, **q):
    return [i["id"] for i in c.get("/api/library", params=q).json()["items"]]


def test_uploader_reads_its_own_private_item_everywhere(api):
    iid = _entry(api)
    ix.Indexer(api.store).run_once()
    c = api.as_(ALICE)
    assert _ids_list(c) == [iid]
    assert _ids_list(c, kind="diary") == [iid]
    assert _ids_list(c, q="dear") == [iid]
    got = c.get(f"/api/library/{iid}")
    assert got.status_code == 200
    assert got.json()["uploaded_by_you"] is True
    assert c.get(f"/api/library/{iid}/file").content.startswith(b"# Dear diary")
    assert "aardvark" in c.get(f"/api/library/{iid}/text").json()["text"]
    assert str(iid) in c.get(f"/api/excerpts?items={iid}").json()["items"]
    hits = c.get("/api/search", params={"q": "aardvark", "mode": "fulltext"}).json()["hits"]
    assert [h["item_id"] for h in hits] == [iid]
    events = c.get("/api/feed").json()["events"]
    assert [(e["type"], e["item_id"]) for e in events] == [("created", iid)]


def test_uploader_sees_own_items_over_opds_and_webdav(api):
    iid = _entry(api)
    c = api.as_(ALICE)
    root = ET.fromstring(c.get("/opds").content)
    assert [e.find(f"{ATOM}title").text for e in root.iter(f"{ATOM}entry")] == ["diary"]
    feed = ET.fromstring(c.get("/opds/diary").content)
    assert len(list(feed.iter(f"{ATOM}entry"))) == 1
    found = ET.fromstring(c.get("/opds/search?q=dear").content)
    assert len(list(found.iter(f"{ATOM}entry"))) == 1
    assert c.get(f"/opds/item/{iid}/file").status_code == 200
    ms = ET.fromstring(c.request("PROPFIND", "/dav/diary/", headers={"Depth": "1"}).content)
    hrefs = [h.text for h in ms.iter(f"{DAV}href")]
    assert any(h.startswith(f"/dav/diary/{iid}-") for h in hrefs)
    name = next(h for h in hrefs if h.startswith(f"/dav/diary/{iid}-"))
    assert c.get(name).status_code == 200


def test_uploader_sends_archives_and_restores_its_own_item(api):
    iid = _entry(api)
    c = api.as_(ALICE)
    assert c.post(f"/api/library/{iid}/send", json={"target": "x3"}).status_code == 202
    assert api.published
    assert api.published[0]["id"] == iid
    assert c.delete(f"/api/library/{iid}").status_code == 204
    assert _ids_list(c) == []
    r = c.post(f"/api/library/{iid}/restore")
    assert r.status_code == 200
    assert r.json()["archived_at"] is None
    types = [e["type"] for e in c.get("/api/feed").json()["events"]]
    assert types == ["created", "send_requested", "archived", "updated"]


def test_other_keys_still_cannot_see_or_touch_it(api):
    iid = _entry(api)
    ix.Indexer(api.store).run_once()
    c = api.as_(BOB)
    assert _ids_list(c) == []
    assert _ids_list(c, q="dear") == []
    for path in (f"/api/library/{iid}", f"/api/library/{iid}/file", f"/api/library/{iid}/text"):
        assert c.get(path).status_code == 404, path
    assert c.get(f"/api/excerpts?items={iid}").json()["items"] == {}
    assert c.get("/api/search", params={"q": "aardvark"}).json()["hits"] == []
    assert c.get("/api/feed").json()["events"] == []
    assert c.get(f"/opds/item/{iid}/file").status_code == 404
    assert len(list(ET.fromstring(c.get("/opds/diary").content).iter(f"{ATOM}entry"))) == 0
    assert c.post(f"/api/library/{iid}/send", json={"target": "x3"}).status_code == 404
    assert c.delete(f"/api/library/{iid}").status_code == 404
    assert c.post(f"/api/library/{iid}/restore").status_code == 404
    # identical bytes: the opaque 409, no id
    r = api.upload(BOB, b"# Dear diary\n\nthe aardvark sleeps", "x.md", kind="diary")
    assert r.status_code == 409
    assert r.json() == {
        "detail": "an item with these bytes already exists",
        "code": "conflict",
    }
    assert api.store.get_item(iid).archived_at is None


def test_nothing_else_widens(api):
    secret = _entry(api, OWNER, b"owner's private diary")
    mine = _entry(api)
    c = api.as_(ALICE)
    assert c.get(f"/api/library/{secret}").status_code == 404
    assert _ids_list(c) == [mine]
    r = api.upload(ALICE, b"not a diary", "d.md", kind="document")
    assert r.status_code == 403
    assert r.json()["code"] == "forbidden_kind"
    # the owner sees both, and is told which one it uploaded
    c = api.as_(OWNER)
    assert c.get(f"/api/library/{secret}").json()["uploaded_by_you"] is True
    assert c.get(f"/api/library/{mine}").json()["uploaded_by_you"] is False


def test_uploader_recorded_and_backfilled_from_feed(tmp_path):
    from culture_library.core import library

    store = Store(tmp_path / "d")
    item, _ = library.add(store, b"x", "x.md", uploader="kid")
    with store.reader() as conn:
        row = conn.execute("SELECT key_id FROM item_uploaders WHERE item_id = ?", (item.id,))
        assert tuple(row.fetchone()) == ("kid",)
    with store.connect() as conn:  # a database from before the table existed
        conn.execute("DELETE FROM item_uploaders")
    reopened = Store(tmp_path / "d")
    assert library.uploader_of(reopened, item.id) == "kid"
    anon, _ = library.add(reopened, b"y", "y.md")
    assert library.uploader_of(reopened, anon.id) is None
