"""The repodoc MCP tools: one table entry per repodoc route, the caller's key, no server key."""

from __future__ import annotations

import base64

import pytest

from culture_library.core import indexer as ix
from culture_library.mcp import tools
from culture_library.mcp.tools import ToolError

SPEC = "# Plan\nnarwhal migration notes\n"
PNG = b"\x89PNG\r\n\x1a\n\xff\xfe\x00binary"


def call(env, raw, name, **args):
    return tools.call_tool(name, args, env.factory(raw))


def repodoc_routes_in_app(env):
    return {
        (method.upper(), path)
        for path, ops in env.app.openapi()["paths"].items()
        if path.startswith(("/api/repodocs", "/api/projects", "/api/domains"))
        for method in ops
    }


@pytest.fixture
def keys(env):
    return (
        env.key("writer", ["read", "write"], private=True),
        env.key("reader", ["read"], private=True),
        env.key("public-reader", ["read"]),
    )


def push(env, key, path="docs/specs/a.md", content=SPEC, **extra):
    body = {
        "remote": "https://github.com/agentculture/alpha",
        "path": path,
        "commit": "c" * 40,
        "content": content,
        "committed_at": "2026-10-30T10:00:00Z",
        "default_branch_head": "c" * 40,
    }
    return call(env, key, "repodoc_push", **{**body, **extra})


def test_every_repodoc_route_has_a_tool_and_back(env):
    declared = list(tools.REPODOC_ROUTES.values())
    assert len(declared) == len(set(declared))
    assert set(declared) == repodoc_routes_in_app(env)
    spec_names = {s["name"] for s in tools.tool_specs()}
    assert {n for n in spec_names if n.startswith("repodoc_")} == set(tools.REPODOC_ROUTES)
    assert set(tools.REPODOC_ALSO_USES) <= set(tools.REPODOC_ROUTES)
    assert set(tools.REPODOC_ALSO_USES.values()) <= repodoc_routes_in_app(env)


def test_push_list_read_links(env, keys):
    writer, reader, public = keys
    out = push(env, writer, project={"domains": ["ai"], "connected": []})
    doc_id = out["doc"]["id"]
    listed = call(env, reader, "repodoc_list", project="https://github.com/agentculture/alpha")[
        "docs"
    ]
    assert [d["id"] for d in listed] == [doc_id]
    assert call(env, reader, "repodoc_list", domain="ai")["docs"]
    assert not call(env, reader, "repodoc_list", domain="nope")["docs"]
    read = call(env, reader, "repodoc_read", doc_id=doc_id)
    assert read["text"] == SPEC and "content_base64" not in read
    assert read["doc"]["id"] == doc_id and read["versions"]
    assert "spec_links" in call(env, reader, "repodoc_links", doc_id=doc_id)
    # private docs stay invisible to a public key
    assert call(env, public, "repodoc_list")["docs"] == []


def test_read_working_and_commit_and_binary(env, keys):
    writer, reader, _ = keys
    doc_id = push(env, writer)["doc"]["id"]
    push(env, writer, commit="working", content=SPEC + "draft line\n")
    assert "draft line" not in call(env, reader, "repodoc_read", doc_id=doc_id)["text"]
    assert (
        "draft line" in call(env, reader, "repodoc_read", doc_id=doc_id, version="working")["text"]
    )
    sha = "c" * 40
    assert call(env, reader, "repodoc_read", doc_id=doc_id, version=sha)["text"] == SPEC
    rendered = call(env, reader, "repodoc_rendered", doc_id=doc_id, version="working")
    assert rendered["doc_id"] == doc_id
    b = push(
        env, writer, path="docs/img.png", content=None, content_b64=base64.b64encode(PNG).decode()
    )
    out = call(env, reader, "repodoc_read", doc_id=b["doc"]["id"])
    assert base64.b64decode(out["content_base64"]) == PNG and "text" not in out


def test_search_takes_corpora(env, keys):
    writer, reader, _ = keys
    push(env, writer)
    ix.enqueue_missing(env.store)
    ix.Indexer(env.store, lambda texts: [[1.0, 0.0] for _ in texts]).run_once()
    hits = call(env, reader, "search", q="narwhal", mode="fulltext", corpora=["repodoc"])["hits"]
    assert hits and all(h["corpus"] == "repodoc" for h in hits)
    assert call(env, reader, "search", q="narwhal", mode="fulltext", corpora=["item"])["hits"] == []


def test_read_key_cannot_push(env, keys):
    _, reader, _ = keys
    with pytest.raises(ToolError, match="403"):
        push(env, reader)


def test_projects_and_domains(env, keys):
    writer, reader, _ = keys
    push(env, writer, project={"domains": ["ai"], "connected": []})
    assert call(env, reader, "repodoc_projects")["projects"]
    assert call(env, reader, "repodoc_domains")["domains"][0]["domain"] == "ai"
