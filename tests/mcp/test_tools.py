"""Tool handlers against the real in-process API; each call uses the caller's own key."""

from __future__ import annotations

import base64

import pytest

from culture_library.mcp import tools
from culture_library.mcp.client import ConfigError, from_env


def call(env, raw, name, **args):
    return tools.call_tool(name, args, env.factory(raw))


def test_specs_cover_every_tool():
    names = {s["name"] for s in tools.tool_specs()}
    assert names == {
        "push_file",
        "list_library",
        "search",
        "read_item",
        "send_to_device",
        "diary_write",
        "diary_edit",
        "diary_delete",
    }
    for s in tools.tool_specs():
        assert s["inputSchema"]["type"] == "object"


def test_push_list_search_read(env):
    key = env.key("agent", ["read", "write"], private=True)
    out = call(env, key, "push_file", content="# Notes\nzebra crossing", filename="n.md",
               kind="document", title="Zebra")  # fmt: skip
    assert out["created"] is True
    assert out["item"]["title"] == "Zebra"
    item_id = out["item"]["id"]
    again = call(env, key, "push_file", content="# Notes\nzebra crossing", filename="n.md")
    assert again["created"] is False
    assert again["item"]["id"] == item_id
    assert [i["id"] for i in call(env, key, "list_library")["items"]] == [item_id]
    assert call(env, key, "list_library", q="Zebra", kind="document")["items"]
    env.app.state.store  # indexer is not running; index text synchronously
    from culture_library.core.indexer import Indexer

    Indexer(env.store).run_once()
    hits = call(env, key, "search", q="zebra", mode="fulltext")["hits"]
    assert hits
    assert hits[0]["item_id"] == item_id
    read = call(env, key, "read_item", item_id=item_id)
    assert read["item"]["id"] == item_id
    assert "zebra crossing" in read["text"]


def test_read_item_binary_is_base64_with_cap(env):
    key = env.key("agent", ["read", "write"], private=True)
    data = b"%PDF-1.4 " + b"x" * 200
    pushed = call(env, key, "push_file", content_base64=base64.b64encode(data).decode(),
                  filename="a.pdf")  # fmt: skip
    iid = pushed["item"]["id"]
    read = call(env, key, "read_item", item_id=iid)
    assert base64.b64decode(read["content_base64"]) == data
    capped = call(env, key, "read_item", item_id=iid, max_bytes=10)
    assert "content_base64" not in capped
    assert capped["truncated"] is True


def test_push_validation(env):
    key = env.key("agent", ["read", "write"])
    with pytest.raises(tools.ToolError, match="exactly one"):
        call(env, key, "push_file", filename="a.md")
    with pytest.raises(tools.ToolError, match="filename"):
        call(env, key, "push_file", content="x")
    with pytest.raises(tools.ToolError, match="base64"):
        call(env, key, "push_file", content_base64="!!", filename="a.pdf")


def test_scopes_are_obeyed(env):
    reader = env.key("ro", ["read"])
    with pytest.raises(tools.ToolError, match="403"):
        call(env, reader, "push_file", content="x", filename="a.md")
    writer = env.key("w", ["read", "write"], kinds=["article"])
    with pytest.raises(tools.ToolError, match="403"):
        call(env, writer, "push_file", content="x", filename="a.md", kind="document")
    assert call(env, writer, "push_file", content="x", filename="a.md", kind="article")["created"]
    nokey = env.factory("clk_deadbeef_nope")
    with pytest.raises(tools.ToolError, match="401"):
        tools.call_tool("list_library", {}, nokey)


def test_send_to_device(env):
    key = env.key("agent", ["read", "write"], private=True)
    iid = call(env, key, "push_file", content="hi", filename="a.md")["item"]["id"]
    out = call(env, key, "send_to_device", item_id=iid, target="x3-ori")
    assert out["status"] == "requested"
    assert env.published
    with pytest.raises(tools.ToolError, match="unknown_target"):
        call(env, key, "send_to_device", item_id=iid, target="nope")
    ro = env.key("ro", ["read"], private=True)
    with pytest.raises(tools.ToolError, match="403"):
        call(env, ro, "send_to_device", item_id=iid, target="x3-ori")


def test_unknown_tool_and_missing_args(env):
    key = env.key("agent", ["read"])
    with pytest.raises(tools.ToolError, match="unknown tool"):
        call(env, key, "nope")
    with pytest.raises(tools.ToolError, match="invalid_arguments"):
        call(env, key, "read_item")


def test_from_env_requires_key():
    with pytest.raises(ConfigError):
        from_env({})
    c = from_env({"CULTURE_LIBRARY_API_KEY": "clk_a_b"})
    assert c.base_url == "http://127.0.0.1:8796"
    assert (
        from_env({"CULTURE_LIBRARY_API_KEY": "k", "CULTURE_LIBRARY_URL": "http://h:1/"}).base_url
        == "http://h:1"
    )
