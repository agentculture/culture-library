"""t10: server-side judge-free runs: start, poll, close out on restart."""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store, eval_store  # noqa: E402
from culture_library.core import indexer as ix  # noqa: E402
from culture_library.core import search as sr  # noqa: E402
from culture_library.eval.models import EvalSet, Sample  # noqa: E402
from culture_library.eval.runner import PRECISION, RECALL, IdOnlyAdapter, run_set  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402
from culture_library.server import routes_eval_jobs as jobs  # noqa: E402
from culture_library.server import routes_search  # noqa: E402
from culture_library.server.auth import Principal, current_principal  # noqa: E402

WORDS = ("whale", "ship", "harpoon", "ocean", "captain")
# One ~700-char paragraph per word, so the 1000-char chunker gives each its own chunk.
TEXT = "\n\n".join(f"The {w} story. " + "and then " * 80 for w in WORDS).encode()


def _embed(texts):
    return [[1.0 if w in t.lower() else 0.0 for w in WORDS] + [0.1] for t in texts]


def _index(library, item):
    with library.store.connect() as conn:
        sr.index_title(conn, item)
    ix.enqueue_missing(library.store)
    ix.Indexer(library.store, _embed).run_once()


@pytest.fixture
def fixture_set(library):
    library.app.state.query_embedder = _embed
    library.app.state.reranker = None
    item = library.add_item(TEXT, title="Leviathan")
    _index(library, item)
    chunks = [c for c in library.client.get_chunks(item.id) if not c.is_header]
    samples = [
        Sample(
            question=f"What about the {w}?",
            reference_answer=c.text,
            reference_chunk_shas=[c.chunk_sha],
        )
        for w, c in zip(WORDS, chunks)
    ]
    assert len(samples) == len(WORDS)
    created = library.client.create_set(
        EvalSet(
            item_id=item.id,
            item_sha256=item.sha256,
            adapter="fake",
            adapter_version="0",
            samples=samples,
        )
    )
    return library.client.get_set(created["id"])


def _wait(library, run_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = library.http.get(f"/api/eval/runs/{run_id}").json()
        if got["status"] != "running":
            return got
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} still running")


def _start(library, **body):
    return library.http.post("/api/eval/runs/start", json=body)


def test_server_run_matches_client_run_set(library, fixture_set):
    r = _start(library, set_id=fixture_set.id, mode="hybrid", k=3)
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    assert r.json()["status"] == "running"
    server = _wait(library, run_id)

    client = run_set(library.client, fixture_set, IdOnlyAdapter(), mode="hybrid", k=3)

    assert server["status"] == client.status == "complete"
    assert server["config"]["latency_source"] == "in-process"
    assert server["config"]["server"] == client.config["server"]
    assert server["total"] == client.total
    assert server["completed"] == client.completed
    by_id = {s["sample_id"]: s for s in server["samples"]}
    for s in client.samples:
        assert by_id[s.sample_id]["retrieved_chunk_shas"] == s.retrieved_chunk_shas
        assert by_id[s.sample_id]["retrieved_item_ids"] == s.retrieved_item_ids
    for key in (PRECISION, RECALL):
        assert server["aggregates"][key] == pytest.approx(client.aggregates[key])
    assert {s["name"] for s in server["suites"]} == {s["name"] for s in client.suites}
    assert "judged" not in {s["name"] for s in server["suites"]}


def test_second_start_while_running_is_409_and_progress_is_reported(library, fixture_set):
    gate = threading.Event()

    def slow(texts):
        gate.wait(10)
        return _embed(texts)

    library.app.state.query_embedder = slow
    first = _start(library, set_id=fixture_set.id)
    assert first.status_code == 202
    run_id = first.json()["run_id"]
    try:
        second = _start(library, set_id=fixture_set.id)
        assert second.status_code == 409
        assert second.json()["code"] == "running"
        assert second.json()["run_id"] == run_id
        got = library.http.get(f"/api/eval/runs/{run_id}").json()
        assert got["status"] == "running"
        assert got["total"] == len(fixture_set.samples)
        assert got["completed"] == 0
    finally:
        gate.set()
    done = _wait(library, run_id)
    assert done["status"] == "complete"
    assert done["completed"] == len(fixture_set.samples)
    again = _start(library, set_id=fixture_set.id)  # no longer running: a new run starts
    assert again.status_code == 202
    _wait(library, again.json()["run_id"])


def test_judged_suite_is_refused_with_a_hint(library, fixture_set):
    r = _start(library, set_id=fixture_set.id, suites=["id_retrieval", "judged"])
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "needs_judge"
    assert "culture-library eval run" in body["hint"]
    with library.store.reader() as conn:
        assert eval_store.list_runs(conn, include_private=True) == []


def test_unknown_suite_and_bad_options_are_refused(library, fixture_set):
    r = _start(library, set_id=fixture_set.id, suites=["nope"])
    assert r.status_code == 422
    assert r.json()["code"] == "invalid"
    assert _start(library, set_id=fixture_set.id, k=0).status_code == 422
    assert _start(library, set_id=fixture_set.id, mode="fts").status_code == 422


def test_chosen_suites_only(library, fixture_set):
    r = _start(library, set_id=fixture_set.id, suites=["speed"])
    assert r.status_code == 202
    got = _wait(library, r.json()["run_id"])
    assert [s["name"] for s in got["suites"]] == ["speed"]


def test_missing_set_404_and_write_scope_needed(library, fixture_set):
    assert _start(library, set_id=9999).status_code == 404
    reader = Principal(key_id="r", scopes=frozenset({"read"}), kinds=None, private=True)
    library.app.dependency_overrides[current_principal] = lambda: reader
    assert _start(library, set_id=fixture_set.id).status_code == 403
    wrong_kind = Principal(
        key_id="w", scopes=frozenset({"read", "write"}), kinds=frozenset({"article"}), private=True
    )
    library.app.dependency_overrides[current_principal] = lambda: wrong_kind
    assert _start(library, set_id=fixture_set.id).status_code == 403


def test_unindexed_item_is_refused_without_a_run(library):
    library.app.state.query_embedder = _embed
    item = library.add_item(b"never indexed", title="Pending")
    created = library.client.create_set(
        EvalSet(
            item_id=item.id,
            item_sha256=item.sha256,
            adapter="fake",
            adapter_version="0",
            samples=[Sample(question="q?", reference_answer="a")],
        )
    )
    r = _start(library, set_id=created["id"])
    assert r.status_code == 409
    assert r.json()["code"] == "stale"
    with library.store.reader() as conn:
        assert eval_store.list_runs(conn, include_private=True) == []


def test_run_retrieves_with_the_starting_keys_visibility(library, fixture_set):
    secret = library.add_item(
        b"The whale and the ship and the harpoon and the captain.",
        title="Secret",
        visibility="private",
    )
    _index(library, secret)
    public_key = Principal(
        key_id="pub", scopes=frozenset({"read", "write"}), kinds=None, private=False
    )
    library.app.dependency_overrides[current_principal] = lambda: public_key
    r = _start(library, set_id=fixture_set.id, k=5)
    assert r.status_code == 202
    got = _wait(library, r.json()["run_id"])
    seen = {i for s in got["samples"] for i in s["retrieved_item_ids"]}
    assert seen
    assert secret.id not in seen


def test_failed_worker_closes_the_run_failed(library, fixture_set, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("worker exploded")

    monkeypatch.setattr(jobs, "run_set", boom)
    r = _start(library, set_id=fixture_set.id)
    got = _wait(library, r.json()["run_id"])
    assert got["status"] == "failed"
    assert "worker exploded" in got["config"]["error"]


# ---- restart close-out


def _running_run(store, set_id, total, *, sample_ids=()):
    with store.connect() as conn:
        rid = eval_store.create_run(
            conn,
            set_id,
            status="partial",
            config={"mode": "hybrid", "latency_source": "in-process"},
            total=total,
            completed=len(sample_ids),
            running=True,
        )
        for sid in sample_ids:
            eval_store.add_run_sample(
                conn,
                rid,
                sid,
                retrieved_chunk_shas=["x"],
                scores={PRECISION: 0.5, RECALL: 1.0},
            )
    return rid


def test_interrupted_runs_are_closed_out_on_restart(library, fixture_set, monkeypatch, tmp_path):
    ids = [s.id for s in fixture_set.samples]
    some = _running_run(library.store, fixture_set.id, len(ids), sample_ids=ids[:2])
    none = _running_run(library.store, fixture_set.id, len(ids))

    monkeypatch.setenv("CULTURE_LIBRARY_PURGE_INTERVAL_S", "0")
    monkeypatch.setattr(routes_search, "start_indexer", lambda store, embedder=None: None)
    monkeypatch.setattr(routes_search, "stop_indexer", lambda: None)
    app = server_app.create_app()
    app.state.store = Store(library.store.data_dir)
    app.state.query_embedder = None
    app.state.reranker = None
    principal = Principal(key_id="x", scopes=frozenset({"read"}), kinds=None, private=True)
    app.dependency_overrides[current_principal] = lambda: principal
    with TestClient(app) as http:
        a = http.get(f"/api/eval/runs/{some}").json()
        b = http.get(f"/api/eval/runs/{none}").json()
    assert a["status"] == "partial"
    assert a["completed"] == 2
    assert a["config"]["closed_reason"] == "server restarted while running"
    assert a["partial_aggregates"] == {PRECISION: 0.5, RECALL: 1.0}
    assert a["aggregates"] == {}
    assert b["status"] == "failed"
    assert b["config"]["closed_reason"] == "server restarted while running"
    assert b["partial_aggregates"] == {}
    with app.state.store.reader() as conn:
        assert eval_store.running_runs(conn) == []


def test_progress_stores_each_finished_question(library, fixture_set):
    gate = threading.Event()
    calls = []

    def third_blocks(texts):
        calls.append(texts)
        if len(calls) == 3:
            gate.wait(10)
        return _embed(texts)

    library.app.state.query_embedder = third_blocks
    run_id = _start(library, set_id=fixture_set.id).json()["run_id"]
    try:
        deadline = time.monotonic() + 10
        while library.http.get(f"/api/eval/runs/{run_id}").json()["completed"] < 2:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        got = library.http.get(f"/api/eval/runs/{run_id}").json()
        assert got["status"] == "running"
        assert got["completed"] == 2
        assert [s["question"] for s in got["samples"]] == [
            s.question for s in fixture_set.samples[:2]
        ]
        assert all(s["latency_ms"] is not None for s in got["samples"])
    finally:
        gate.set()
    assert _wait(library, run_id)["status"] == "complete"


def test_client_run_is_not_trivial(library, fixture_set):
    run = run_set(library.client, fixture_set, IdOnlyAdapter(), mode="hybrid", k=3)
    assert run.aggregates[RECALL] > 0
    assert len({tuple(s.retrieved_chunk_shas) for s in run.samples}) > 1


def test_failed_run_keeps_its_results_and_says_how_many(library, fixture_set, monkeypatch):
    """A run that fails after its retrievals closes with completed = the results it keeps."""

    def retrieve_then_boom(*a, **kw):
        jobs_run_set(*a, **kw)
        raise RuntimeError("suite exploded")

    jobs_run_set = jobs.run_set
    monkeypatch.setattr(jobs, "run_set", retrieve_then_boom)
    got = _wait(library, _start(library, set_id=fixture_set.id).json()["run_id"])
    assert got["status"] == "failed"
    assert got["completed"] == len(got["samples"]) == len(fixture_set.samples)


def test_on_retrieval_reports_every_question_once_and_skips_warmup(library, fixture_set):
    calls = []
    run_set(
        library.client,
        fixture_set,
        IdOnlyAdapter(),
        k=3,
        warmup=True,
        on_retrieval=lambda i, s, p, ms, err: calls.append((i, s.question, p is not None, err)),
    )
    assert calls == [(i, s.question, True, None) for i, s in enumerate(fixture_set.samples)]


def test_warmup_server_run_stores_every_question(library, fixture_set):
    got = _wait(library, _start(library, set_id=fixture_set.id, warmup=True).json()["run_id"])
    assert got["status"] == "complete"
    assert [s["question"] for s in got["samples"]] == [s.question for s in fixture_set.samples]


def test_a_failing_progress_reporter_does_not_end_the_run(library, fixture_set):
    def boom(*_a):
        raise RuntimeError("database is locked")

    run = run_set(library.client, fixture_set, IdOnlyAdapter(), k=3, on_retrieval=boom)
    assert run.status == "complete"
    assert run.completed == len(fixture_set.samples)


def _progress_run(library, fixture_set):
    with library.store.connect() as conn:
        return eval_store.create_run(
            conn,
            fixture_set.id,
            status="partial",
            config={},
            total=len(fixture_set.samples),
            completed=0,
            running=True,
        )


def test_progress_counts_only_committed_results(library, fixture_set, monkeypatch):
    run_id = _progress_run(library, fixture_set)
    progress = jobs._Progress(library.store, run_id, fixture_set.version)
    sample = fixture_set.samples[0]
    progress(0, sample, None, None, "timeout")  # a failed retrieval is not a result
    assert progress.recorded == 0
    assert progress.errors == [{"sample_id": sample.id, "error": "timeout"}]

    def locked(*_a, **_kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(eval_store, "set_run_progress", locked)
    with pytest.raises(sqlite3.OperationalError):
        progress(0, sample, [], 1.0, None)
    assert progress.recorded == 0  # rolled back, so not counted
    with library.store.reader() as conn:
        assert eval_store.list_run_samples(conn, run_id) == []


def test_failed_run_keeps_the_questions_whose_retrieval_failed(library, fixture_set, monkeypatch):
    real_retrieve, real_run_set = jobs.InProcessClient.retrieve, jobs.run_set
    first = fixture_set.samples[0].question

    def retrieve(self, query, *a, **kw):
        if query == first:
            raise jobs.EvalApiError("gateway down", status=503)
        return real_retrieve(self, query, *a, **kw)

    def retrieve_then_boom(*a, **kw):
        real_run_set(*a, **kw)
        raise RuntimeError("suite exploded")

    monkeypatch.setattr(jobs.InProcessClient, "retrieve", retrieve)
    monkeypatch.setattr(jobs, "run_set", retrieve_then_boom)
    got = _wait(library, _start(library, set_id=fixture_set.id).json()["run_id"])
    assert got["status"] == "failed"
    assert got["completed"] == len(got["samples"]) == len(fixture_set.samples) - 1
    errors = got["config"]["errors"]
    assert [e["sample_id"] for e in errors] == [fixture_set.samples[0].id]
