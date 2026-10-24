"""A diary-scoped key manages its own diary entries only; delete archives."""

from __future__ import annotations

import pytest

from culture_library.mcp import tools


def call(env, raw, name, **args):
    return tools.call_tool(name, args, env.factory(raw))


@pytest.fixture
def diary_key(env):
    return env.key("diary", ["read", "write"], kinds=["diary"], private=True)


def test_diary_write_creates_markdown_diary_item(env, diary_key):
    out = call(env, diary_key, "diary_write", text="Dear diary\nToday was fine.")
    item = out["item"]
    assert item["kind"] == "diary"
    assert item["format"] == "md"
    assert item["visibility"] == "private"
    assert item["title"] == "Dear diary"
    body = env.http.get(
        f"/api/library/{item['id']}/file", headers={"Authorization": f"Bearer {diary_key}"}
    )
    assert body.content.decode().startswith("Dear diary\nToday was fine.")


def test_diary_edit_replaces_and_archives_old(env, diary_key):
    old = call(env, diary_key, "diary_write", text="first draft")["item"]["id"]
    out = call(env, diary_key, "diary_edit", entry_id=old, text="second draft")
    new = out["item"]["id"]
    assert new != old
    assert out["replaces"] == old
    assert out["archived"] == old
    ids = [i["id"] for i in call(env, diary_key, "list_library", kind="diary")["items"]]
    assert ids == [new]
    body = env.http.get(
        f"/api/library/{new}/file", headers={"Authorization": f"Bearer {diary_key}"}
    ).text
    assert "second draft" in body
    assert f"replaces: {old}" in body


def test_diary_delete_archives_for_30_days(env, diary_key):
    iid = call(env, diary_key, "diary_write", text="secret")["item"]["id"]
    out = call(env, diary_key, "diary_delete", entry_id=iid)
    assert out["archived"] == iid
    assert out["restorable_days"] == 30
    assert call(env, diary_key, "list_library")["items"] == []
    row = env.store.get_item(iid)
    assert row is not None
    assert row.archived_at  # archived, not purged


def test_other_kinds_are_refused(env, diary_key):
    admin = env.key("admin", ["read", "write"], private=True)
    book = call(env, admin, "push_file", content="x", filename="a.md", kind="document")
    bid = book["item"]["id"]
    for name, extra in (("diary_edit", {"text": "y"}), ("diary_delete", {})):
        with pytest.raises(tools.ToolError):
            call(env, diary_key, name, entry_id=bid, **extra)
    assert env.store.get_item(bid).archived_at is None
    with pytest.raises(tools.ToolError, match="403"):
        call(env, diary_key, "push_file", content="x", filename="b.md", kind="document")


def test_other_keys_diary_entries_are_refused(env, diary_key):
    other = env.key("other", ["read", "write"], kinds=["diary"], private=True)
    iid = call(env, other, "diary_write", text="not yours")["item"]["id"]
    with pytest.raises(tools.ToolError, match="not_owner"):
        call(env, diary_key, "diary_delete", entry_id=iid)
    with pytest.raises(tools.ToolError, match="not_owner"):
        call(env, diary_key, "diary_edit", entry_id=iid, text="hijack")
    assert env.store.get_item(iid).archived_at is None
    # the owner still can
    assert call(env, other, "diary_delete", entry_id=iid)["archived"] == iid


def test_diary_errors_on_missing_entry_and_readonly_key(env, diary_key):
    with pytest.raises(tools.ToolError, match="404"):
        call(env, diary_key, "diary_delete", entry_id=999)
    iid = call(env, diary_key, "diary_write", text="x")["item"]["id"]
    ro = env.key("ro", ["read"], private=True)
    with pytest.raises(tools.ToolError):
        call(env, ro, "diary_delete", entry_id=iid)
    with pytest.raises(tools.ToolError, match="invalid_arguments"):
        call(env, diary_key, "diary_write", text="  ")


# -- a diary-scoped key WITHOUT private access (d4 item 2) ----------------------


@pytest.fixture
def plain_diary_key(env):
    return env.key("plain-diary", ["read", "write"], kinds=["diary"], private=False)


def test_non_private_diary_key_manages_its_own_entries(env, plain_diary_key):
    k = plain_diary_key
    item = call(env, k, "diary_write", text="Morning pages\nthe heron waits")["item"]
    assert item["visibility"] == "private"
    read = call(env, k, "read_item", item_id=item["id"])
    assert read["text"].startswith("Morning pages")
    listed = call(env, k, "list_library", kind="diary")["items"]
    assert [i["id"] for i in listed] == [item["id"]]
    edited = call(env, k, "diary_edit", entry_id=item["id"], text="Evening pages")
    new = edited["item"]["id"]
    assert edited["archived"] == item["id"]
    assert edited["item"]["visibility"] == "private"
    assert [i["id"] for i in call(env, k, "list_library")["items"]] == [new]
    assert call(env, k, "read_item", item_id=new)["text"].startswith("Evening pages")
    assert call(env, k, "diary_delete", entry_id=new)["archived"] == new
    assert call(env, k, "list_library")["items"] == []


def test_non_private_diary_key_cannot_see_others_private_entries(env, plain_diary_key):
    other = env.key("other", ["read", "write"], kinds=["diary"], private=False)
    theirs = call(env, other, "diary_write", text="their secret")["item"]["id"]
    for name, extra in (
        ("read_item", {"item_id": theirs}),
        ("diary_edit", {"entry_id": theirs, "text": "hijack"}),
        ("diary_delete", {"entry_id": theirs}),
    ):
        with pytest.raises(tools.ToolError, match="404"):
            call(env, plain_diary_key, name, **extra)
    assert call(env, plain_diary_key, "list_library")["items"] == []
    assert env.store.get_item(theirs).archived_at is None
    # identical bytes of another key's private entry: still the opaque 409
    with pytest.raises(tools.ToolError, match="409"):
        call(
            env, plain_diary_key, "push_file", content="their secret", filename="d.md", kind="diary"
        )


def test_ownership_check_does_not_scan_the_feed(env, diary_key, monkeypatch):
    iid = call(env, diary_key, "diary_write", text="x")["item"]["id"]
    seen = []
    real = env._send

    def spy(method, url, headers, body):
        seen.append(url)
        return real(method, url, headers, body)

    monkeypatch.setattr(env, "_send", spy)
    call(env, diary_key, "diary_delete", entry_id=iid)
    assert not any("/api/feed" in u for u in seen)
