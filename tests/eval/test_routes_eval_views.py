"""routes_eval_views: radar, compare and weakest over stored runs (plan t9)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from culture_library.server.auth import Principal, current_principal  # noqa: E402


def _as(library, **kw):
    p = Principal(key_id=kw.pop("key_id", "k2"), scopes=frozenset(kw.pop("scopes", {"read"})), **kw)
    library.app.dependency_overrides[current_principal] = lambda: p


def _make_set(library, item, urls=None):
    samples = [
        {
            "question": f"q{i}?",
            "reference_chunk_shas": [f"sha{i}"],
            "source_urls": (urls or {}).get(i, []),
        }
        for i in range(2)
    ]
    body = {
        "item_id": item.id,
        "item_sha256": item.sha256,
        "adapter": "fake",
        "samples": samples,
    }
    r = library.http.post("/api/eval/sets", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _make_run(library, s, hits=(True, True), judge=None, **extra):
    """A complete run; hit -> reference retrieved at rank 1, else a miss."""
    ids = [x["id"] for x in s["samples"]]
    results = []
    for n, (sid, hit) in enumerate(zip(ids, hits)):
        results.append(
            {
                "sample_id": sid,
                "retrieved_chunk_shas": [f"sha{n}"] if hit else ["other"],
                "retrieved_item_ids": [s["item_id"]],
                "question": f"q{n}?",
                "reference_chunk_shas": [f"sha{n}"],
                "latency_ms": 100,
                "scores": {},
            }
        )
    body = {
        "set_id": s["id"],
        "status": "complete",
        "config": {"k": 5, "judge_model": "j"},
        "total": 2,
        "completed": 2,
        "aggregates": judge or {},
        "error_rate": 0.0,
        "suites": ["id_retrieval"],
        "samples": results,
        **extra,
    }
    r = library.http.post("/api/eval/runs", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_radar_single_run(library):
    s = _make_set(library, library.add_item(b"a"))
    rid = _make_run(library, s, hits=(True, False), judge={"context_recall": 0.5})
    r = library.http.get(f"/api/eval/radar?run={rid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "run" and body["left_out"] == 0 and body["budget_s"] == 1.0
    assert body["axes"]["found_at_1"] == 0.5
    assert body["axes"]["judge_recall"] == 0.5
    assert body["axes"]["no_errors"] == 1.0
    assert body["axes"]["latency_in_budget"] == 1.0
    assert body["documents"][0]["run_id"] == rid and body["documents"][0]["questions"] == 2
    # budget is a parameter
    tight = library.http.get(f"/api/eval/radar?run={rid}&budget_s=0.05").json()
    assert tight["axes"]["latency_in_budget"] == 0.0


def test_radar_group_is_mean_over_documents(library):
    r1 = _make_run(library, _make_set(library, library.add_item(b"a")), hits=(True, True))
    r2 = _make_run(library, _make_set(library, library.add_item(b"b")), hits=(False, False))
    body = library.http.get(f"/api/eval/radar?runs={r1},{r2}").json()
    assert body["kind"] == "group" and len(body["documents"]) == 2
    assert body["axes"]["found_at_1"] == 0.5


def test_radar_pair(library):
    r1 = _make_run(library, _make_set(library, library.add_item(b"a")), hits=(True, True))
    r2 = _make_run(library, _make_set(library, library.add_item(b"b")), hits=(False, False))
    body = library.http.get(f"/api/eval/radar?run={r1}&vs={r2}").json()
    assert body["kind"] == "pair"
    assert body["a"]["axes"]["found_at_1"] == 1.0 and body["b"]["axes"]["found_at_1"] == 0.0


def test_item_aware_matching_uses_set_item(library):
    s = _make_set(library, library.add_item(b"a"))
    ids = [x["id"] for x in s["samples"]]
    body = {
        "set_id": s["id"],
        "config": {"k": 5},
        "samples": [
            {
                "sample_id": ids[0],
                "retrieved_chunk_shas": ["sha0"],
                "retrieved_item_ids": [s["item_id"] + 999],  # same sha, other book
                "reference_chunk_shas": ["sha0"],
                "question": "q0?",
            }
        ],
    }
    rid = library.http.post("/api/eval/runs", json=body).json()["id"]
    assert library.http.get(f"/api/eval/radar?run={rid}").json()["axes"]["found_at_5"] == 0.0


def test_bad_params_422(library):
    s = _make_set(library, library.add_item(b"a"))
    rid = _make_run(library, s)
    for q in (
        "/api/eval/radar",
        f"/api/eval/radar?run={rid}&runs={rid}",
        "/api/eval/radar?run=abc",
        f"/api/eval/radar?run={rid},{rid + 1}",
        f"/api/eval/radar?run={rid}&budget_s=0",
        f"/api/eval/weakest?runs={rid}&limit=0",
        f"/api/eval/weakest?run={rid}&runs={rid}",
        "/api/eval/compare?before=x&after=1",
    ):
        r = library.http.get(q)
        assert r.status_code == 422, (q, r.text)
        assert r.json()["code"] == "invalid"


def test_scope_required(library):
    s = _make_set(library, library.add_item(b"a"))
    rid = _make_run(library, s)
    _as(library, scopes={"write"}, private=True)
    for q in (
        f"/api/eval/radar?run={rid}",
        f"/api/eval/compare?before={rid}&after={rid}",
        f"/api/eval/weakest?runs={rid}",
    ):
        assert library.http.get(q).status_code == 403, q
    _as(library, scopes={"read"}, private=True)
    assert library.http.get(f"/api/eval/radar?run={rid}").status_code == 200


def test_invisible_run_is_404_alone_everywhere(library):
    s = _make_set(library, library.add_item(b"secret", visibility="private", title="p"))
    rid = _make_run(library, s)
    _as(library, scopes={"read"}, private=False)
    for q in (
        f"/api/eval/radar?run={rid}",
        f"/api/eval/radar?run={rid}&vs={rid}",
        f"/api/eval/compare?before={rid}&after={rid}",
        f"/api/eval/weakest?run={rid}",
        "/api/eval/radar?run=9999",
    ):
        r = library.http.get(q)
        assert r.status_code == 404, q
        assert "q0" not in r.text


def test_group_leaves_out_invisible_runs(library):
    pub = _make_run(library, _make_set(library, library.add_item(b"a")), hits=(False, False))
    priv = _make_run(
        library,
        _make_set(library, library.add_item(b"s", visibility="private", title="p")),
        hits=(True, True),
    )
    _as(library, scopes={"read"}, private=False)
    r = library.http.get(f"/api/eval/radar?runs={pub},{priv},9999")
    assert r.status_code == 200
    body = r.json()
    assert body["left_out"] == 2  # the private run and the unknown one look the same
    assert [d["run_id"] for d in body["documents"]] == [pub]
    assert body["axes"]["found_at_1"] == 0.0  # no value from the hidden run
    wk = library.http.get(f"/api/eval/weakest?runs={pub},{priv}").json()
    assert wk["left_out"] == 1 and {i["run_id"] for i in wk["items"]} == {pub}
    cmp = library.http.get(f"/api/eval/compare?before={pub},{priv}&after={pub}").json()
    assert cmp["left_out"] == {"before": 1, "after": 0}


def test_group_all_hidden_is_empty_not_404(library):
    priv = _make_run(
        library, _make_set(library, library.add_item(b"s", visibility="private", title="p"))
    )
    _as(library, scopes={"read"}, private=False)
    body = library.http.get(f"/api/eval/radar?runs={priv}").json()
    assert body["documents"] == [] and body["left_out"] == 1
    assert all(v is None for v in body["axes"].values())


def test_compare_pair_and_deltas(library):
    s = _make_set(library, library.add_item(b"a"))
    before = _make_run(library, s, hits=(True, False))
    after = _make_run(library, s, hits=(True, True))
    body = library.http.get(f"/api/eval/compare?before={before}&after={after}").json()
    assert body["checks"]["comparable"] is True
    assert body["deltas"]["found_at_1"] == 0.5
    assert body["before"]["found_at_1"] == 0.5 and body["after"]["found_at_1"] == 1.0
    assert [c["question"] for c in body["changed_questions"]] == ["q1?"]
    assert body["left_out"] == {"before": 0, "after": 0}


def test_compare_not_comparable_returns_only_checks(library):
    s = _make_set(library, library.add_item(b"a"))
    a = _make_run(library, s)
    b = _make_run(library, s, config={"k": 5, "judge_model": "other"})
    body = library.http.get(f"/api/eval/compare?before={a}&after={b}").json()
    assert body["checks"]["comparable"] is False
    assert any("judge" in x for x in body["checks"]["reasons"])
    assert "deltas" not in body and "before" not in body


def test_compare_warns_on_different_set_and_depth(library):
    a = _make_run(library, _make_set(library, library.add_item(b"a")))
    b = _make_run(
        library,
        _make_set(library, library.add_item(b"b")),
        config={"k": 3, "judge_model": "j"},
    )
    w = library.http.get(f"/api/eval/compare?before={a}&after={b}").json()["checks"]["warnings"]
    assert any("different set" in x for x in w) and any("depth" in x for x in w)


def test_compare_groups_pair_by_set(library):
    s1 = _make_set(library, library.add_item(b"a"))
    s2 = _make_set(library, library.add_item(b"b"))
    b1, b2 = _make_run(library, s1, hits=(False, False)), _make_run(library, s2, hits=(True, True))
    a1, a2 = _make_run(library, s1, hits=(True, True)), _make_run(library, s2, hits=(True, True))
    body = library.http.get(f"/api/eval/compare?before={b1},{b2}&after={a1},{a2}").json()
    assert body["checks"]["comparable"] is True and body["checks"]["warnings"] == []
    assert body["before"]["found_at_1"] == 0.5 and body["after"]["found_at_1"] == 1.0
    assert body["deltas"]["found_at_1"] == 0.5
    assert {c["set_id"] for c in body["changed_questions"]} == {s1["id"]}
    # a group with a different judge on one set is not comparable
    bad = _make_run(library, s2, config={"k": 5, "judge_model": "x"})
    nc = library.http.get(f"/api/eval/compare?before={b1},{b2}&after={a1},{bad}").json()
    assert nc["checks"]["comparable"] is False and "deltas" not in nc
    # no overlapping sets at all
    s3 = _make_set(library, library.add_item(b"c"))
    c3 = _make_run(library, s3)
    none = library.http.get(f"/api/eval/compare?before={b1},{b2}&after={c3},{c3}").json()
    assert none["checks"]["comparable"] is False


def test_compare_group_with_no_visible_side(library):
    pub = _make_run(library, _make_set(library, library.add_item(b"a")))
    priv = _make_run(
        library, _make_set(library, library.add_item(b"s", visibility="private", title="p"))
    )
    _as(library, scopes={"read"}, private=False)
    body = library.http.get(f"/api/eval/compare?before={priv},{priv + 50}&after={pub}").json()
    assert body["checks"]["comparable"] is False and body["left_out"]["before"] == 2


def test_weakest_sources_and_items(library):
    s = _make_set(
        library,
        library.add_item(b"paper", kind="newspaper"),
        urls={0: ["http://good"], 1: ["http://bad"]},
    )
    rid = _make_run(library, s, hits=(True, False))
    body = library.http.get(f"/api/eval/weakest?runs={rid}").json()
    assert [x["url"] for x in body["sources"]] == ["http://bad", "http://good"]
    assert body["sources"][0]["run_id"] == rid and body["sources"][0]["axes"]["found_at_5"] == 0.0
    assert body["items"][0]["item_id"] == s["item_id"]
    assert body["items"][0]["questions"] == 2
    one = library.http.get(f"/api/eval/weakest?run={rid}&limit=1").json()
    assert len(one["sources"]) == 1 and one["sources"][0]["url"] == "http://bad"
    assert one["limit"] == 1
