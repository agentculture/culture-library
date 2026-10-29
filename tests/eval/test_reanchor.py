"""Re-anchoring eval references after a re-index (core/eval_reanchor + the reanchor route)."""

from __future__ import annotations

import pytest

from culture_library.core import eval_reanchor as ra
from culture_library.core import search as sr
from culture_library.core.search import Chunk

STORY = ["Call me Ishmael. Some years ago I went to sea.", "The whale surfaced at dawn."]
LICENCE = "*** START OF THE PROJECT GUTENBERG EBOOK ***\nThis eBook is for the use of anyone."


def _chunks(texts):
    return [Chunk(n, sr.chunk_sha(t), t, n == 0) for n, t in enumerate(texts)]


# ------------------------------------------------------------------ core


def test_norm_collapses_whitespace_and_case_is_kept():
    assert ra.normalise("  a \n\t b  ") == "a b"


def test_context_inside_chunk_maps_to_it():
    chunks = _chunks(["Title", "Intro words. The whale surfaced at dawn. And more words."])
    got = ra.chunks_for_context("the whale surfaced   at dawn.".replace("the", "The"), chunks)
    assert got == [chunks[1].chunk_sha]


def test_context_spanning_chunks_maps_to_each_contained_chunk():
    chunks = _chunks(["Title", "First half.", "Second half."])
    got = ra.chunks_for_context("First half.\n\nSecond half.", chunks)
    assert got == [chunks[1].chunk_sha, chunks[2].chunk_sha]


def test_header_chunk_never_matches_by_reverse_containment():
    chunks = _chunks(["T", "unrelated body"])
    assert ra.chunks_for_context("T is a letter in a long context", chunks) == []


def test_reanchor_samples_stale_when_any_context_missing_or_no_context():
    chunks = _chunks(["Title", *STORY])
    samples = [
        {
            "id": 1,
            "question": "q1",
            "reference_contexts": [STORY[0]],
            "reference_chunk_shas": ["x"],
        },
        {"id": 2, "question": "q2", "reference_contexts": [LICENCE], "reference_chunk_shas": ["y"]},
        {"id": 3, "question": "q3", "reference_contexts": [STORY[1], LICENCE]},
        {"id": 4, "question": "q4", "reference_contexts": []},
    ]
    res = ra.reanchor_samples(samples, chunks)
    assert res.updates == {1: [chunks[1].chunk_sha]}
    assert [s["sample_id"] for s in res.stale] == [2, 3, 4]
    assert res.stale[0]["reason"] == "reference text not found in current chunks"
    assert res.stale[1]["reason"] == "reference text not found in current chunks"
    assert res.stale[2]["reason"] == "no stored reference text"


# ------------------------------------------------------------------ route

pytest.importorskip("fastapi")


def _index_item(library, texts):
    item = library.add_item("\n\n".join(texts).encode(), title="Moby")
    with library.store.connect() as conn:
        sr.set_embeddings(conn, item.id, texts, [[1.0, 0.0]] * len(texts), "m")
    return item


def _build_set(library, item, chunks):
    samples = [
        {
            "question": f"q{i}?",
            "reference_answer": "a",
            "reference_chunk_shas": [c.chunk_sha],
            "reference_contexts": [c.text],
        }
        for i, c in enumerate(chunks)
    ]
    body = {
        "item_id": item.id,
        "item_sha256": item.sha256,
        "adapter": "fake",
        "adapter_version": "0",
        "samples": samples,
    }
    r = library.http.post("/api/eval/sets", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_reanchor_after_licence_removed(library):
    before = ["Moby\nHerman", LICENCE, *STORY]
    item = _index_item(library, before)
    old = _chunks(before)
    s = _build_set(library, item, old[1:])  # licence, story0, story1
    sample_ids = [x["id"] for x in s["samples"]]
    # re-chunk: licence gone, story re-split so story0 is now inside a bigger chunk
    after = ["Moby\nHerman", STORY[0] + "\n\n" + STORY[1]]
    with library.store.connect() as conn:
        sr.set_embeddings(conn, item.id, after, [[1.0, 0.0]] * len(after), "m")
    new = _chunks(after)

    r = library.http.post(f"/api/eval/sets/{s['id']}/reanchor")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 2 and body["previous_version"] == 1
    assert body["re_anchored"] == sample_ids[1:]
    assert [x["sample_id"] for x in body["could_not_re_anchor"]] == sample_ids[:1]
    assert body["note"]["stale"] == 1 and body["note"]["re_anchored"] == 2

    got = library.http.get(f"/api/eval/sets/{s['id']}").json()
    assert got["version"] == 2
    assert got["generator"]["reanchor"]["version"] == 2
    by_id = {x["id"]: x for x in got["samples"]}
    assert set(by_id) == set(sample_ids)  # sample ids kept
    assert by_id[sample_ids[1]]["reference_chunk_shas"] == [new[1].chunk_sha]
    assert by_id[sample_ids[2]]["reference_chunk_shas"] == [new[1].chunk_sha]
    assert by_id[sample_ids[0]]["reference_chunk_shas"] == [old[1].chunk_sha]  # stale untouched


def test_reanchor_unchanged_does_not_bump_version(library):
    texts = ["Moby", *STORY]
    item = _index_item(library, texts)
    s = _build_set(library, item, _chunks(texts)[1:])
    body = library.http.post(f"/api/eval/sets/{s['id']}/reanchor").json()
    assert body["version"] == 1 and body["could_not_re_anchor"] == []
    assert library.http.get(f"/api/eval/sets/{s['id']}").json()["version"] == 1


def test_reanchor_errors(library):
    assert library.http.post("/api/eval/sets/999/reanchor").status_code == 404
    item = library.add_item(b"unindexed")
    s = _build_set(library, item, _chunks(["x"]))
    r = library.http.post(f"/api/eval/sets/{s['id']}/reanchor")
    assert r.status_code == 409 and r.json()["code"] == "not_indexed"


def test_reanchor_needs_write_scope(library):
    from culture_library.server.auth import Principal, current_principal

    texts = ["Moby", *STORY]
    item = _index_item(library, texts)
    s = _build_set(library, item, _chunks(texts)[1:])
    p = Principal(key_id="r", scopes=frozenset({"read"}), private=True)
    library.app.dependency_overrides[current_principal] = lambda: p
    assert library.http.post(f"/api/eval/sets/{s['id']}/reanchor").status_code == 403
