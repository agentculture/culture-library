"""``discord-message`` items (#19) through the HTTP surface: upload, reader text, OPDS, WebDAV."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from culture_library.server.auth import Principal  # noqa: E402
from tests.server.test_library_api import Api  # noqa: E402

BODY = b"# #general\n\n**alice**: hello from Discord\n\nhttps://discord.com/channels/1/2/3\n"


def _writer(*kinds: str) -> Principal:
    return Principal("k", frozenset({"write", "read"}), kinds=frozenset(kinds), private=True)


@pytest.fixture
def api(tmp_path):
    return Api(tmp_path)


def _push(api):
    return api.upload(
        BODY, "m.md", kind="discord-message", format="md", visibility="private", title="msg"
    )


def test_upload_allowed_key_is_201(api):
    api.as_(_writer("discord-message"))
    r = _push(api)
    assert r.status_code == 201
    assert r.json()["item"]["kind"] == "discord-message"
    assert r.json()["item"]["visibility"] == "private"


def test_upload_key_without_the_kind_is_403(api):
    api.as_(_writer("document"))
    r = _push(api)
    assert r.status_code == 403
    assert r.json()["code"] == "forbidden_kind"


def test_reader_text_returns_the_markdown_body(api):
    api.as_(_writer("discord-message"))
    iid = _push(api).json()["item"]["id"]
    r = api.c.get(f"/api/library/{iid}/text")
    assert r.status_code == 200
    assert "hello from Discord" in r.json()["text"]
    assert api.c.get(f"/api/library/{iid}", params={}).json()["kind"] == "discord-message"


def test_opds_feed_and_catalog_list_the_kind(api):
    api.as_(_writer("discord-message"))
    _push(api)
    cat = api.c.get("/opds")
    assert cat.status_code == 200 and "discord-message" in cat.text
    feed = api.c.get("/opds/discord-message")
    assert feed.status_code == 200 and "msg" in feed.text


def test_webdav_lists_the_kind(api):
    api.as_(_writer("discord-message"))
    _push(api)
    root = api.c.request("PROPFIND", "/dav/", headers={"Depth": "1"})
    assert root.status_code == 207 and "/dav/discord-message/" in root.text
    folder = api.c.request("PROPFIND", "/dav/discord-message/", headers={"Depth": "1"})
    assert folder.status_code == 207 and "msg" in folder.text
