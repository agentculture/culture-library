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


# --- eval suites: new run fields, snapshots, old body, catalog (plan t8) ---------------------


def _two(library):
    s = _make_set(library, library.add_item(b"x"))
    return s, [x["id"] for x in s["samples"]]


def test_post_run_old_body_still_accepted_with_empty_new_fields(library):
    s, ids = _two(library)
    body = {
        "set_id": s["id"],
        "status": "complete",
        "config": {"mode": "hybrid"},
        "total": 2,
        "completed": 2,
        "aggregates": {"hit": 0.5},
        "samples": [
            {"sample_id": i, "retrieved_chunk_shas": ["sha0"], "scores": {"hit": 1.0}} for i in ids
        ],
    }
    r = library.http.post("/api/eval/runs", json=body)
    assert r.status_code == 201, r.text
    got = r.json()
    assert got["status"] == "complete"
    assert got["aggregates"] == {"hit": 0.5}
    assert got["partial_aggregates"] == {}
    assert got["error_rate"] is None
    assert got["suites"] == []
    assert got["samples"][0]["question"] is None
    assert got["samples"][0]["latency_ms"] is None


def test_post_run_new_fields_roundtrip(library):
    s, ids = _two(library)
    body = {
        "set_id": s["id"],
        "status": "partial",
        "total": 2,
        "completed": 1,
        "partial_aggregates": {"recall": 0.75},
        "error_rate": 0.5,
        "suites": [{"name": "id_retrieval", "version": 2}, "judged"],
        "samples": [
            {
                "sample_id": ids[0],
                "retrieved_chunk_shas": ["a"],
                "scores": {"recall": 0.75},
                "question": "q0?",
                "reference_chunk_shas": ["sha0"],
                "set_version": 1,
                "retrieved_item_ids": [7, 8],
                "latency_ms": 12.5,
                "freshness": {"stale": False},
            }
        ],
    }
    r = library.http.post("/api/eval/runs", json=body)
    assert r.status_code == 201, r.text
    got = library.http.get(f"/api/eval/runs/{r.json()['id']}").json()
    assert got["status"] == "partial"
    assert got["aggregates"] == {}
    assert got["partial_aggregates"] == {"recall": 0.75}
    assert got["error_rate"] == 0.5
    assert got["suites"] == [
        {"name": "id_retrieval", "version": 2},
        {"name": "judged", "version": None},
    ]
    smp = got["samples"][0]
    assert smp["question"] == "q0?"
    assert smp["reference_chunk_shas"] == ["sha0"]
    assert smp["retrieved_item_ids"] == [7, 8]
    assert smp["latency_ms"] == 12.5
    assert smp["freshness"] == {"stale": False}
    assert smp["set_version"] == 1


def test_partial_aggregates_do_not_trip_aggregate_rule(library):
    s, _ = _two(library)
    r = library.http.post(
        "/api/eval/runs",
        json={"set_id": s["id"], "status": "failed", "partial_aggregates": {"x": 1.0}},
    )
    assert r.status_code == 201
    assert r.json()["aggregates"] == {}


@pytest.mark.parametrize(
    "patch",
    [
        {"error_rate": "lots"},
        {"error_rate": 1.5},
        {"suites": [3]},
        {"suites": "id_retrieval"},
        {"partial_aggregates": {"x": "high"}},
        {"samples": [{"sample_id": 1, "latency_ms": "slow"}]},
        {"samples": [{"sample_id": 1, "retrieved_item_ids": ["a"]}]},
        {"samples": [{"sample_id": 1, "question": 5}]},
    ],
)
def test_bad_new_field_types_rejected(library, patch):
    s, _ = _two(library)
    r = library.http.post("/api/eval/runs", json={"set_id": s["id"], **patch})
    assert r.status_code == 422, r.text


def test_running_flag_reports_status_running_with_progress(library):
    from culture_library.core import eval_store

    s, _ = _two(library)
    with library.store.connect() as conn:
        rid = eval_store.create_run(
            conn, s["id"], status="partial", config={}, total=4, completed=1, running=True
        )
    got = library.http.get(f"/api/eval/runs/{rid}").json()
    assert got["status"] == "running"
    assert (got["completed"], got["total"]) == (1, 4)
    listed = library.http.get("/api/eval/runs").json()["runs"]
    assert listed[0]["status"] == "running"


def test_suite_catalog(library):
    from culture_library.eval import suites

    r = library.http.get("/api/eval/suites")
    assert r.status_code == 200
    assert r.json() == {"suites": suites.catalog()}
    names = {x["name"] for x in r.json()["suites"]}
    assert names
    first = r.json()["suites"][0]
    assert {"name", "version", "needs_judge", "metrics"} <= set(first)
    assert {"name", "definition", "range", "direction"} <= set(first["metrics"][0])


def test_suite_catalog_needs_read_scope(library):
    _as(library, scopes={"write"})
    assert library.http.get("/api/eval/suites").status_code == 403
