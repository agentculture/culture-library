"""routes_eval: store and read eval sets and runs, filtered by item visibility."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from culture_library.eval.models import EvalRun, EvalSet, Sample, SampleResult  # noqa: E402
from culture_library.server.auth import Principal, current_principal  # noqa: E402


def _set_body(item, n=2):
    return EvalSet(
        item_id=item.id,
        item_sha256=item.sha256,
        adapter="fake",
        adapter_version="0",
        chunker={"size": 100},
        generator={"model": "m"},
        samples=[
            Sample(question=f"q{i}?", reference_answer=f"a{i}", reference_chunk_shas=[f"sha{i}"])
            for i in range(n)
        ],
    ).to_dict()


def _make_set(library, item):
    r = library.http.post("/api/eval/sets", json=_set_body(item))
    assert r.status_code in (200, 201), r.text
    return r.json()


def _as(library, **kw):
    p = Principal(key_id=kw.pop("key_id", "k2"), scopes=frozenset(kw.pop("scopes", {"read"})), **kw)
    library.app.dependency_overrides[current_principal] = lambda: p


def test_post_set_stores_and_roundtrips(library):
    item = library.add_item(b"hello world")
    body = _make_set(library, item)
    assert body["id"]
    assert body["version"] == 1
    assert len(body["samples"]) == 2
    assert all(s["id"] for s in body["samples"])
    full = library.http.get(f"/api/eval/sets/{body['id']}").json()
    parsed = EvalSet.from_dict(full)
    assert parsed.item_id == item.id
    assert parsed.chunker == {"size": 100}
    assert [s.question for s in parsed.samples] == ["q0?", "q1?"]


def test_list_sets_summaries_without_samples(library):
    item = library.add_item(b"x")
    other = library.add_item(b"y", title="o")
    _make_set(library, item)
    _make_set(library, other)
    sets = library.http.get(f"/api/eval/sets?item_id={item.id}").json()["sets"]
    assert len(sets) == 1
    assert sets[0]["item_id"] == item.id
    assert "samples" not in sets[0]
    assert len(library.http.get("/api/eval/sets").json()["sets"]) == 2


def test_post_run_roundtrip_and_aggregate_rule(library):
    item = library.add_item(b"x")
    s = _make_set(library, item)
    ids = [x["id"] for x in s["samples"]]
    run = EvalRun(
        set_id=s["id"],
        status="complete",
        config={"mode": "hybrid"},
        total=2,
        completed=2,
        aggregates={"hit": 0.5},
        samples=[SampleResult(i, ["sha0"], {"hit": 1.0}) for i in ids],
    )
    r = library.http.post("/api/eval/runs", json=run.to_dict())
    assert r.status_code == 201, r.text
    stored = r.json()
    assert stored["id"]
    assert stored["set_version"] == 1
    assert stored["aggregates"] == {"hit": 0.5}
    got = library.http.get(f"/api/eval/runs/{stored['id']}").json()
    assert got["samples"][0]["scores"] == {"hit": 1.0}
    assert got["config"] == {"mode": "hybrid"}
    runs = library.http.get(f"/api/eval/runs?set_id={s['id']}").json()["runs"]
    assert [x["id"] for x in runs] == [stored["id"]]
    assert "samples" not in runs[0]


@pytest.mark.parametrize("status", ["partial", "failed"])
def test_aggregates_refused_unless_complete(library, status):
    s = _make_set(library, library.add_item(b"x"))
    bad = EvalRun(set_id=s["id"], status=status, total=2, completed=1, aggregates={"hit": 1.0})
    assert library.http.post("/api/eval/runs", json=bad.to_dict()).status_code == 422
    ok = EvalRun(set_id=s["id"], status=status, total=2, completed=1)
    r = library.http.post("/api/eval/runs", json=ok.to_dict())
    assert r.status_code == 201
    assert r.json()["aggregates"] in ({}, None)


def test_bad_status_and_foreign_sample_422(library):
    s = _make_set(library, library.add_item(b"x"))
    assert (
        library.http.post(
            "/api/eval/runs", json=EvalRun(set_id=s["id"], status="weird").to_dict()
        ).status_code
        == 422
    )
    run = EvalRun(set_id=s["id"], samples=[SampleResult(99999, [], {})])
    assert library.http.post("/api/eval/runs", json=run.to_dict()).status_code == 422


def test_unknown_set_and_item_404(library):
    body = _set_body(library.add_item(b"x"))
    body["item_id"] = 9999
    assert library.http.post("/api/eval/sets", json=body).status_code == 404
    assert (
        library.http.post("/api/eval/runs", json=EvalRun(set_id=9999).to_dict()).status_code == 404
    )
    assert library.http.get("/api/eval/sets/9999").status_code == 404
    assert library.http.get("/api/eval/runs/9999").status_code == 404


def test_invisible_item_hidden_everywhere(library):
    item = library.add_item(b"secret text", visibility="private", title="p")
    s = _make_set(library, item)
    run = library.http.post("/api/eval/runs", json=EvalRun(set_id=s["id"]).to_dict()).json()
    _as(library, scopes={"read", "write"}, private=False)
    assert library.http.get("/api/eval/sets").json()["sets"] == []
    assert library.http.get(f"/api/eval/sets?item_id={item.id}").json()["sets"] == []
    assert library.http.get("/api/eval/runs").json()["runs"] == []
    r = library.http.get(f"/api/eval/sets/{s['id']}")
    assert r.status_code == 404
    assert "q0" not in r.text
    assert library.http.get(f"/api/eval/runs/{run['id']}").status_code == 404
    assert library.http.post("/api/eval/sets", json=_set_body(item)).status_code == 404
    assert (
        library.http.post("/api/eval/runs", json=EvalRun(set_id=s["id"]).to_dict()).status_code
        == 404
    )


def test_scopes_and_kind_restriction(library):
    item = library.add_item(b"x", kind="book")
    s = _make_set(library, item)
    _as(library, scopes={"read"}, private=True)
    assert library.http.post("/api/eval/sets", json=_set_body(item)).status_code == 403
    assert (
        library.http.post("/api/eval/runs", json=EvalRun(set_id=s["id"]).to_dict()).status_code
        == 403
    )
    assert library.http.get("/api/eval/sets").status_code == 200
    _as(library, scopes={"write"}, private=True)
    assert library.http.get("/api/eval/sets").status_code == 403
    _as(library, scopes={"read", "write"}, kinds=frozenset({"article"}), private=True)
    assert library.http.post("/api/eval/sets", json=_set_body(item)).status_code == 403
    assert (
        library.http.post("/api/eval/runs", json=EvalRun(set_id=s["id"]).to_dict()).status_code
        == 403
    )
    assert library.http.get(f"/api/eval/sets/{s['id']}").status_code == 200
