"""The eval MCP tools: one tool per eval route, the caller's key, the route's own scope check."""

from __future__ import annotations

import hashlib

import pytest

from culture_library.mcp import tools
from culture_library.mcp.tools import ToolError

TEXT = b"# Whale\nwhale tale\n"


def call(env, raw, name, **args):
    return tools.call_tool(name, args, env.factory(raw))


@pytest.fixture
def keys(env):
    return env.key("writer", ["read", "write"]), env.key("reader", ["read"])


@pytest.fixture
def seeded(env, keys):
    """An item, a set and a complete run, all made through MCP tools."""
    writer, _ = keys
    item = call(
        env, writer, "push_file", content=TEXT.decode(), filename="w.md", visibility="public"
    )["item"]
    s = call(
        env,
        writer,
        "eval_create_set",
        item_id=item["id"],
        item_sha256=hashlib.sha256(TEXT).hexdigest(),
        adapter="fake",
        samples=[{"question": "q0?", "reference_chunk_shas": ["sha0"]}],
    )
    sid = s["samples"][0]["id"]
    run = call(
        env,
        writer,
        "eval_post_run",
        set_id=s["id"],
        config={"k": 5},
        total=1,
        completed=1,
        error_rate=0.0,
        suites=["id_retrieval"],
        samples=[
            {
                "sample_id": sid,
                "retrieved_chunk_shas": ["sha0"],
                "retrieved_item_ids": [item["id"]],
                "question": "q0?",
                "reference_chunk_shas": ["sha0"],
                "latency_ms": 10,
            }
        ],
    )
    return s, run


def eval_routes_in_app(env):
    """(method, path) for every /api/eval route, read from the app's OpenAPI schema."""
    return {
        (method.upper(), path)
        for path, ops in env.app.openapi()["paths"].items()
        if path.startswith("/api/eval")
        for method in ops
    }


def test_every_eval_route_has_a_tool_and_back(env):
    declared = list(tools.EVAL_ROUTES.values())
    assert len(declared) == len(set(declared))
    assert set(declared) == eval_routes_in_app(env)
    spec_names = {s["name"] for s in tools.tool_specs()}
    assert set(tools.EVAL_ROUTES) <= spec_names
    assert {n for n in spec_names if n.startswith("eval_")} == set(tools.EVAL_ROUTES)


def test_read_key_reads_but_cannot_write(env, keys, seeded):
    _, reader = keys
    s, run = seeded
    assert call(env, reader, "eval_suites")["suites"]
    assert [x["id"] for x in call(env, reader, "eval_sets")["sets"]] == [s["id"]]
    assert call(env, reader, "eval_set", set_id=s["id"])["id"] == s["id"]
    assert [r["id"] for r in call(env, reader, "eval_runs", set_id=s["id"])["runs"]] == [run["id"]]
    assert call(env, reader, "eval_run", run_id=run["id"])["status"] == "complete"
    writes = {
        "eval_create_set": {"item_id": 1, "item_sha256": "x", "adapter": "fake"},
        "eval_post_run": {"set_id": s["id"]},
        "eval_start_run": {"set_id": s["id"]},
        "eval_reanchor": {"set_id": s["id"]},
    }
    for name, args in writes.items():
        with pytest.raises(ToolError, match="403"):
            call(env, reader, name, **args)


def test_suites_to_radar_with_mcp_alone(env, keys, seeded):
    _, reader = keys
    _, run = seeded
    assert call(env, reader, "eval_suites")["suites"]
    radar = call(env, reader, "eval_radar", run=run["id"])
    assert radar["axes"]["found_at_1"] == 1.0
    again = call(env, reader, "eval_radar", runs=[run["id"]])
    assert again["axes"]["found_at_1"] == 1.0
    cmp_ = call(env, reader, "eval_compare", before=run["id"], after=[run["id"]])
    assert cmp_
    weak = call(env, reader, "eval_weakest", run=run["id"], limit=3)
    direct = env.http.get(
        f"/api/eval/weakest?run={run['id']}&limit=3",
        headers={"Authorization": f"Bearer {keys[1]}"},
    ).json()
    assert weak == direct


def test_tools_return_route_errors(env, keys):
    _, reader = keys
    with pytest.raises(ToolError, match="404"):
        call(env, reader, "eval_run", run_id=999)
    with pytest.raises(ToolError, match="invalid_arguments"):
        call(env, reader, "eval_compare", before=1)
    with pytest.raises(ToolError, match="HTTP 4"):
        call(env, reader, "eval_radar")  # neither run nor runs


def test_start_run_unknown_set_is_404_for_writer(env, keys):
    writer, _ = keys
    with pytest.raises(ToolError, match="404"):
        call(env, writer, "eval_start_run", set_id=999)
