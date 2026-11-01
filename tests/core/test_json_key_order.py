"""jsonb reorders object keys (shorter first); reads restore alphabetical order at every depth."""

from __future__ import annotations

import json

from culture_library.core import eval_store, sources
from culture_library.core.feed import append, read_since
from culture_library.core.jsonvalues import sorted_keys
from culture_library.core.projects import Projects

SHA = "a" * 64
# "b" sorts after "aa" alphabetically but before it in jsonb's length-first order.
RAW = {"b": 1, "aa": {"d": 1, "cc": [{"z": 1, "yy": 2}], "bbb": 3}}
ALPHA = json.dumps(RAW, sort_keys=True)


def test_sorted_keys_every_depth():
    assert json.dumps(sorted_keys(RAW)) == ALPHA


def test_feed_event_data_alphabetical(store):
    with store.connect() as conn:
        append(
            conn,
            {"type": "created", "item_id": 1, "sha256": SHA, "kind": "book", "data": RAW},
        )
        events = read_since(conn, 0, include_private=True)["events"]
    assert json.dumps(events[0]["data"]) == ALPHA


def test_sources_manifest_alphabetical(store):
    item = store.add_item(b"src\n", kind="newspaper", title="t", format="md").item
    with store.connect() as conn:
        sources.put(conn, item.id, [RAW])
        manifest, _ = sources.get_with_meta(conn, item.id)
        assert json.dumps(sources.get(conn, item.id)) == json.dumps([json.loads(ALPHA)])
    assert json.dumps(manifest) == json.dumps([json.loads(ALPHA)])


def test_project_lists_round_trip(store):
    projects = Projects(store)
    projects.upsert("github.com/agentculture/x", ["b", "aa"], ["github.com/agentculture/y"])
    got = projects.get("github.com/agentculture/x")
    assert got["domains"] == ["b", "aa"]
    assert got["connected"] == ["github.com/agentculture/y"]


def test_eval_store_uses_shared_helper():
    assert eval_store.sorted_keys is sorted_keys
