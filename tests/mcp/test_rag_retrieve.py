"""The ``rag_retrieve`` MCP tool: the caller's key, the real ``POST /api/rag/retrieve``, the
server payload returned unchanged, and a 503 surfaced as a clear tool error."""

from __future__ import annotations

import pytest

from culture_library.core import indexer as ix
from culture_library.mcp import tools


def call(env, raw, name, **args):
    return tools.call_tool(name, args, env.factory(raw))


def _embed(texts):
    return [[1.0, 0.0] if "whale" in t.lower() else [0.0, 1.0] for t in texts]


@pytest.fixture
def seeded(env):
    env.app.state.query_embedder = _embed
    env.app.state.reranker = None
    key = env.key("agent", ["read", "write"], private=True)
    whale = call(env, key, "push_file", content="# Whale\nwhale tale", filename="w.md")
    call(env, key, "push_file", content="# Tulips\nflowers", filename="t.md")
    ix.enqueue_missing(env.store)
    ix.Indexer(env.store, _embed).run_once()
    return key, whale["item"]["id"]


def test_rag_retrieve_is_specced(env):
    spec = next(s for s in tools.tool_specs() if s["name"] == "rag_retrieve")
    props = spec["inputSchema"]["properties"]
    assert set(props) == {
        "query",
        "k",
        "mode",
        "kind",
        "item_ids",
        "rerank",
        "candidates",
        "corpora",
    }
    assert spec["inputSchema"]["required"] == ["query"]
    assert spec["inputSchema"]["additionalProperties"] is False
    assert props["mode"]["enum"] == ["semantic", "hybrid"]
    assert props["k"] == {"type": "integer", "minimum": 1, "maximum": 50}
    assert props["candidates"] == {"type": "integer", "minimum": 1, "maximum": 200}
    assert props["rerank"] == {"type": "boolean"}
    assert props["item_ids"]["type"] == "array"


def test_rag_retrieve_returns_server_payload_unchanged(env, seeded):
    key, whale_id = seeded
    out = call(env, key, "rag_retrieve", query="whale", k=1, mode="semantic")
    direct = env.http.post(
        "/api/rag/retrieve",
        json={"query": "whale", "k": 1, "mode": "semantic"},
        headers={"Authorization": f"Bearer {key}"},
    ).json()
    assert out == direct
    assert out["mode"] == "semantic"
    assert out["reranked"] is False
    assert [p["item_id"] for p in out["passages"]] == [whale_id]


def test_rag_retrieve_forwards_filters_and_candidates(env, seeded):
    key, whale_id = seeded
    out = call(
        env,
        key,
        "rag_retrieve",
        query="whale",
        k=2,
        item_ids=[whale_id],
        rerank=False,
        candidates=5,
    )
    assert out["passages"]
    assert {p["item_id"] for p in out["passages"]} == {whale_id}


def test_rag_retrieve_uses_callers_key(env, seeded):
    other = env.key("reader", ["read"])  # not private: must not see the private item
    out = call(env, other, "rag_retrieve", query="whale", k=5)
    assert out["passages"] == []


def test_rag_retrieve_403_is_a_tool_error(env):
    """Keys always carry read (minting refuses a key with no scope), so a 403 is stubbed."""
    from culture_library.mcp.client import Client

    body = b'{"detail": {"detail": "needs the read scope", "code": "forbidden"}}'
    client = Client("http://testserver", "clk_a_b", transport=lambda *a: (403, body))
    with pytest.raises(tools.ToolError, match="forbidden") as info:
        tools.call_tool("rag_retrieve", {"query": "whale"}, lambda: client)
    assert "403" in str(info.value)


def test_rag_retrieve_503_is_a_tool_error(env, seeded):
    key, _ = seeded
    env.app.state.query_embedder = None  # no query embedder configured -> 503
    with pytest.raises(tools.ToolError, match="rag_unavailable") as info:
        call(env, key, "rag_retrieve", query="whale")
    assert "503" in str(info.value)


def test_rag_retrieve_requires_query(env):
    key = env.key("agent", ["read"])
    with pytest.raises(tools.ToolError, match="invalid_arguments"):
        call(env, key, "rag_retrieve", k=3)
