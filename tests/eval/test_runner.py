"""t13: eval/runner.py — run a set against /api/rag/retrieve and score it."""

from __future__ import annotations

import json

import pytest

from culture_library.eval import suites
from culture_library.eval.client import LibraryClient
from culture_library.eval.config import JudgeConfig, OpenAIChat
from culture_library.eval.errors import EvalError
from culture_library.eval.models import EvalSet, Sample, ScoreResult
from culture_library.eval.runner import PRECISION, RECALL, EvalStaleError, run_set
from culture_library.eval.suites import HIGHER, Metric, SuiteScore

from .conftest import FakeAdapter

SHA = "a" * 64
RETRIEVAL = {
    "embed_model": "emb-1",
    "query_prompt": "query",
    "document_prompt": "document",
    "chunker": {"name": "md", "size": 800},
}


class FakeLibrary:
    """A dict-backed library API behind a LibraryClient transport."""

    def __init__(self):
        self.items = {
            1: {"id": 1, "visibility": "public", "sha256": SHA},
            2: {"id": 2, "visibility": "private", "sha256": "b" * 64},
        }
        self.chunks_info = {"item_sha256": SHA, "index_state": "done", "chunks": []}
        self.passages: dict[str, list[dict]] = {}
        self.fail_on: set[str] = set()
        self.retrieve_bodies: list[dict] = []
        self.item_gets: list[int] = []
        self.client = LibraryClient("http://lib", "k", transport=self.transport)

    def transport(self, method, url, headers, body):
        path = url[len("http://lib") :]
        if method == "GET" and path == "/api/status":
            return 200, json.dumps({"retrieval": RETRIEVAL, "items": {}}).encode()
        if method == "GET" and path.startswith("/api/library/"):
            iid = int(path.rsplit("/", 1)[1])
            self.item_gets.append(iid)
            if iid not in self.items:
                return 404, b'{"error": "not_found"}'
            return 200, json.dumps(self.items[iid]).encode()
        if method == "GET" and path == "/api/rag/items/1/chunks":
            return 200, json.dumps(self.chunks_info).encode()
        if method == "POST" and path == "/api/rag/retrieve":
            req = json.loads(body)
            self.retrieve_bodies.append(req)
            if req["query"] in self.fail_on:
                return 500, b'{"error": "boom"}'
            return 200, json.dumps({"passages": self.passages.get(req["query"], [])}).encode()
        return 404, b"{}"


def _p(sha, item_id=1, text="public text"):
    return {"item_id": item_id, "chunk_sha": sha, "text": text}


def _set(*samples):
    return EvalSet(
        item_id=1,
        item_sha256=SHA,
        adapter="fake",
        adapter_version="0",
        samples=list(samples),
        id=7,
        version=1,
    )


@pytest.fixture
def lib():
    return FakeLibrary()


# ---- pre-check


def test_changed_item_runs_and_freshness_reports_it(lib):
    """t7 (planned behaviour change): a changed item no longer refuses the run (it used to
    raise EvalStaleError); the freshness suite reports the questions whose references left."""
    lib.chunks_info["item_sha256"] = "c" * 64
    lib.chunks_info["chunks"] = [{"chunk_no": 0, "chunk_sha": "s1", "text": "t"}]
    lib.passages = {"q1": [_p("s1")], "q2": [_p("s1")]}
    eval_set = _set(
        Sample(question="q1", reference_chunk_shas=["s1"], id=1),
        Sample(question="q2", reference_chunk_shas=["gone"], id=2),
    )
    run = run_set(lib.client, eval_set, FakeAdapter())
    assert run.status == "complete"
    assert len(lib.retrieve_bodies) == 2
    assert [s.freshness for s in run.samples] == ["fresh", "stale"]
    assert run.aggregates["stale_count"] == 1.0
    assert run.aggregates["fresh_share"] == 0.5
    assert run.config["item_changed"] == {"set_sha256": SHA, "item_sha256": "c" * 64}


def test_refuses_when_item_archived_or_missing(lib):
    del lib.items[1]
    eval_set, adapter = _set(Sample(question="q")), FakeAdapter()
    with pytest.raises(EvalStaleError) as ei:
        run_set(lib.client, eval_set, adapter)
    assert "archived" in str(ei.value)
    assert ei.value.hint


def test_refuses_when_index_not_done(lib):
    lib.chunks_info["index_state"] = "pending"
    eval_set, adapter = _set(Sample(question="q")), FakeAdapter()
    with pytest.raises(EvalStaleError):
        run_set(lib.client, eval_set, adapter)
    assert lib.retrieve_bodies == []


# ---- per sample retrieval + ID metrics


def test_posts_run_config_and_computes_id_metrics(lib):
    lib.passages = {
        "q1": [_p("s1"), _p("s2"), _p("x1"), _p("x2")],
        "q2": [_p("y1")],
    }
    s1 = Sample(question="q1", reference_chunk_shas=["s1", "s2", "s3", "s4"], id=11)
    s2 = Sample(question="q2", reference_chunk_shas=["z"], id=12)
    adapter = FakeAdapter()
    run = run_set(
        lib.client, _set(s1, s2), adapter, mode="semantic", k=4, rerank=True, candidates=20
    )
    assert lib.retrieve_bodies[0] == {
        "query": "q1",
        "k": 4,
        "mode": "semantic",
        "rerank": True,
        "candidates": 20,
    }
    assert run.status == "complete"
    assert run.total == run.completed == 2
    assert run.set_id == 7
    r1, r2 = run.samples
    assert r1.sample_id == 11
    assert r1.retrieved_chunk_shas == ["s1", "s2", "x1", "x2"]
    assert r1.scores["id_based_context_precision"] == pytest.approx(0.5)
    assert r1.scores["id_based_context_recall"] == pytest.approx(0.5)
    assert r2.scores["id_based_context_precision"] == 0.0
    assert r2.scores["id_based_context_recall"] == 0.0
    assert run.aggregates["id_based_context_precision"] == pytest.approx(0.25)
    assert run.aggregates["id_based_context_recall"] == pytest.approx(0.25)
    # no judge configured -> adapter's LLM scoring never runs
    assert adapter.score_calls == 0


def test_omits_unset_rerank_and_candidates(lib):
    run_set(lib.client, _set(Sample(question="q")), FakeAdapter())
    assert lib.retrieve_bodies == [{"query": "q", "k": 5, "mode": "hybrid"}]


def test_llm_metrics_via_adapter_when_judge_configured(lib, fake_judge_server):
    lib.passages = {"q": [_p("s1")]}
    adapter = FakeAdapter()
    judge = fake_judge_server.config()
    run = run_set(
        lib.client,
        _set(Sample(question="q", reference_chunk_shas=["s1"])),
        adapter,
        llm=OpenAIChat(judge),
        judge=judge,
    )
    assert adapter.score_calls == 1
    assert run.samples[0].scores["hit"] == 1.0
    assert run.aggregates["hit"] == 1.0
    assert run.aggregates["id_based_context_recall"] == 1.0
    assert run.config["judge_model"] == "cortex"


# ---- privacy


class TextJudgeAdapter(FakeAdapter):
    """Sends every passage text and question to the judge, like an LLM metric would."""

    def score(self, samples, retrieved, llm) -> ScoreResult:
        for s, ps in zip(samples, retrieved):
            llm.complete(
                [{"role": "user", "content": s.question + "\n" + "\n".join(p.text for p in ps)}]
            )
        return ScoreResult(per_sample=[{"faith": 1.0} for _ in samples], aggregate={"faith": 1.0})


def test_non_local_judge_never_sees_private_passage_text(lib, fake_judge_server):
    lib.passages = {"q": [_p("s1", 1, "PUBLIC-ALPHA"), _p("p1", 2, "SECRET-BETA")]}
    judge = JudgeConfig(url="https://judge.example.com", model="remote")
    llm = OpenAIChat(fake_judge_server.config())  # the wire we observe
    run = run_set(
        lib.client,
        _set(Sample(question="q", reference_chunk_shas=["s1"])),
        TextJudgeAdapter(),
        llm=llm,
        judge=judge,
    )
    sent = json.dumps(fake_judge_server.payloads)
    assert fake_judge_server.payloads
    assert "PUBLIC-ALPHA" in sent
    assert "SECRET-BETA" not in sent
    assert run.status == "complete"
    # visibility looked up once per item
    assert lib.item_gets.count(2) == 1


def test_local_judge_may_see_private_text(lib, fake_judge_server):
    lib.passages = {"q": [_p("p1", 2, "SECRET-BETA")]}
    judge = fake_judge_server.config()
    run_set(
        lib.client,
        _set(Sample(question="q")),
        TextJudgeAdapter(),
        llm=OpenAIChat(judge),
        judge=judge,
    )
    assert "SECRET-BETA" in json.dumps(fake_judge_server.payloads)


def test_non_local_judge_skips_llm_metrics_for_private_set_item(lib, fake_judge_server):
    lib.items[1]["visibility"] = "private"
    lib.passages = {"q": [_p("s1", 1, "PRIVATE-SET-TEXT")]}
    judge = JudgeConfig(url="https://judge.example.com")
    run = run_set(
        lib.client,
        _set(Sample(question="q", reference_chunk_shas=["s1"])),
        TextJudgeAdapter(),
        llm=OpenAIChat(fake_judge_server.config()),
        judge=judge,
    )
    assert fake_judge_server.payloads == []
    assert run.status == "complete"
    assert "llm_metrics_skipped" in run.config
    assert run.aggregates["id_based_context_recall"] == 1.0


# ---- failure


def test_failure_on_later_sample_is_partial(lib):
    """t7: the failed question is recorded (empty retrieval, its error) instead of being
    dropped, and the run is partial with completed = successful retrievals."""
    lib.passages = {"q0": [_p("s")], "q1": [_p("s")]}
    lib.fail_on = {"q2"}
    samples = [Sample(question=f"q{i}", reference_chunk_shas=["s"], id=i + 1) for i in range(3)]
    run = run_set(lib.client, _set(*samples), FakeAdapter())
    assert run.status == "partial"
    assert run.completed == 2
    assert run.total == 3
    assert run.aggregates is None
    assert len(run.samples) == 3
    assert run.samples[2].retrieved_chunk_shas == []
    assert run.to_dict()["aggregates"] is None


def test_failure_on_first_sample_keeps_scoring(lib):
    """t7: a failed first retrieval no longer stops the run (it used to end it as failed)."""
    lib.passages = {"q1": [_p("s")]}
    lib.fail_on = {"q0"}
    run = run_set(
        lib.client,
        _set(Sample(question="q0", id=1), Sample(question="q1", reference_chunk_shas=["s"], id=2)),
        FakeAdapter(),
    )
    assert [b["query"] for b in lib.retrieve_bodies] == ["q0", "q1"]
    assert run.status == "partial"
    assert run.completed == 1
    assert run.error_rate == 0.5
    assert run.partial_aggregates["found_at_1"] == 1.0


def _twenty(lib, fail=("q7",)):
    lib.fail_on = set(fail)
    lib.passages = {f"q{i}": [_p("ref", 1), _p(f"x{i}", 2)] for i in range(20)}
    lib.chunks_info["chunks"] = [{"chunk_no": 0, "chunk_sha": "ref", "text": "t"}]
    samples = [
        Sample(question=f"q{i}", reference_chunk_shas=["ref"], id=100 + i) for i in range(20)
    ]
    return run_set(lib.client, _set(*samples), FakeAdapter(), k=5)


def test_one_of_twenty_failing_is_partial_over_nineteen(lib):
    run = _twenty(lib)
    assert len(lib.retrieve_bodies) == 20  # kept going after q7 failed
    assert run.status == "partial"
    assert run.total == 20
    assert run.completed == 19
    assert run.error_rate == pytest.approx(0.05)
    assert run.aggregates is None
    agg = run.partial_aggregates
    # retrieval-quality suites aggregate over the 19 successes, not 20 (19/20 would be 0.95)
    assert agg["found_at_1"] == 1.0
    assert agg["mrr"] == 1.0
    assert agg["distinct_sources"] == 2.0
    # the speed suite's error rate is over every question
    assert agg["error_rate"] == pytest.approx(0.05)
    assert agg["latency_p50_ms"] is not None
    failed = run.samples[7]
    assert failed.sample_id == 107
    assert failed.retrieved_chunk_shas == []
    assert failed.retrieved_item_ids == []
    assert failed.latency_ms is None
    assert failed.scores["error_rate"] == 1.0
    assert "found_at_1" not in failed.scores
    assert failed.freshness == "fresh"
    assert run.config["errors"] == [
        {"sample_id": 107, "error": "POST /api/rag/retrieve -> HTTP 500"}
    ]
    body = run.to_dict()
    assert body["aggregates"] is None
    assert body["partial_aggregates"]["found_at_1"] == 1.0
    assert body["error_rate"] == pytest.approx(0.05)


def test_run_that_scores_nothing_is_failed(lib):
    run = _twenty(lib, fail=tuple(f"q{i}" for i in range(20)))
    assert run.status == "failed"
    assert run.completed == 0
    assert run.error_rate == 1.0
    assert run.aggregates is None
    assert run.partial_aggregates is None
    assert len(run.samples) == 20
    assert run.to_dict()["aggregates"] is None


def test_complete_run_records_suites_snapshots_and_latency(lib):
    run = _twenty(lib, fail=())
    assert run.status == "complete"
    assert run.completed == run.total == 20
    assert run.error_rate == 0.0
    assert run.partial_aggregates is None
    assert {s["name"] for s in run.suites} == {
        "freshness",
        "id_retrieval",
        "result_quality",
        "speed",
    }
    assert all(s["version"] for s in run.suites)
    assert run.config["k"] == 5
    s0 = run.samples[0]
    assert s0.question == "q0"
    assert s0.reference_chunk_shas == ["ref"]
    assert s0.set_version == 1
    assert s0.retrieved_chunk_shas == ["ref", "x0"]
    assert s0.retrieved_item_ids == [1, 2]
    assert isinstance(s0.latency_ms, float) and s0.latency_ms >= 0
    assert s0.freshness == "fresh"
    for key in ("found_at_1", "ndcg", "duplicate_rate", "fresh_share", "error_rate", PRECISION):
        assert key in s0.scores
    for key in ("found_at_1", "duplicate_rate", "fresh_share", "latency_p95_ms", RECALL):
        assert key in run.aggregates


def test_runs_only_the_requested_suites(lib):
    lib.passages = {"q": [_p("s1")]}
    run = run_set(
        lib.client,
        _set(Sample(question="q", reference_chunk_shas=["s1"])),
        FakeAdapter(),
        suites=["id_retrieval"],
    )
    assert run.suites == [{"name": "id_retrieval", "version": "1"}]
    assert set(run.samples[0].scores) == {PRECISION, RECALL} | {
        "found_at_1",
        "found_at_3",
        "found_at_5",
        "found_at_10",
        "precision_at_k",
        "mrr",
        "ndcg",
    }
    assert run.samples[0].freshness is None


def test_unknown_suite_is_an_eval_error(lib):
    with pytest.raises(EvalError, match="unknown suite"):
        run_set(lib.client, _set(Sample(question="q")), FakeAdapter(), suites=["nope"])
    assert lib.retrieve_bodies == []


def test_runs_a_suite_added_to_the_registry(lib):
    class Echo:
        name = "echo_t7"
        version = "3"
        needs_judge = False
        metrics = (Metric("echo_k", "the run's k", (0.0, None), HIGHER),)

        def score(self, ctx):
            return SuiteScore([{"echo_k": float(ctx.k)} for _ in ctx.samples], {"echo_k": 1.0})

    with suites.registered(Echo()):
        run = run_set(lib.client, _set(Sample(question="q")), FakeAdapter(), k=4)
    assert {"name": "echo_t7", "version": "3"} in run.suites
    assert run.samples[0].scores["echo_k"] == 4.0
    assert run.aggregates["echo_k"] == 1.0


def test_judged_requested_without_judge_is_skipped(lib):
    run = run_set(lib.client, _set(Sample(question="q")), FakeAdapter(), suites=["judged", "speed"])
    assert run.status == "complete"
    assert run.config["suites_skipped"] == {"judged": "no judge configured"}
    assert run.config["llm_metrics_skipped"] == "no judge configured"
    assert [s["name"] for s in run.suites] == ["speed"]


def test_warmup_is_one_untimed_retrieve_and_recorded(lib):
    lib.passages = {"q": [_p("s1")]}
    run = run_set(
        lib.client,
        _set(Sample(question="q", reference_chunk_shas=["s1"])),
        FakeAdapter(),
        warmup=True,
    )
    assert [b["query"] for b in lib.retrieve_bodies] == ["q", "q"]
    assert run.config["warmup"] is True
    assert run.completed == 1


def test_no_warmup_by_default(lib):
    run = run_set(lib.client, _set(Sample(question="q")), FakeAdapter())
    assert len(lib.retrieve_bodies) == 1
    assert "warmup" not in run.config


def test_llm_scoring_failure_keeps_complete_id_metrics(lib, fake_judge_server):
    """Every sample was retrieved and ID-scored: the run is complete with ID aggregates only,
    and the LLM failure is recorded rather than turning the whole run partial."""

    class Boom(FakeAdapter):
        def score(self, samples, retrieved, llm):
            raise ValueError("judge said nonsense")

    judge = fake_judge_server.config()
    llm = OpenAIChat(judge)
    eval_set = _set(Sample(question="q"))
    run = run_set(lib.client, eval_set, Boom(), llm=llm, judge=judge)
    assert run.status == "complete"
    # t7: the judge-free suites' metrics are now there too; no LLM-judged metric is
    judged = {"context_precision", "context_recall", "hit"}
    assert {PRECISION, RECALL} <= set(run.aggregates)
    assert not judged & set(run.aggregates)
    assert "judge said nonsense" in run.config["llm_metrics_failed"]
    assert all({PRECISION, RECALL} <= set(s.scores) for s in run.samples)
    assert all(not judged & set(s.scores) for s in run.samples)
    assert "judged" not in [s["name"] for s in run.suites]


# ---- config


def test_config_records_server_retrieval_and_adapter(lib):
    run = run_set(lib.client, _set(Sample(question="q")), FakeAdapter(), k=3)
    assert run.config == {
        "mode": "hybrid",
        "k": 3,
        "rerank": None,
        "candidates": None,
        "adapter": {"name": "fake", "version": "0"},
        "judge_model": None,
        "server": RETRIEVAL,
    }


# ---- posting (client) against the real routes


def test_create_run_posts_new_fields_to_the_real_routes(library):
    from culture_library.core.indexer import Indexer, enqueue

    def embed(texts):
        return [[1.0, 0.0] for _ in texts]

    library.app.state.query_embedder = embed
    library.app.state.reranker = None
    item = library.add_item(
        "\n\n".join(f"Paragraph {i}. " + "words here. " * 30 for i in range(4)).encode()
    )
    with library.store.connect() as conn:
        enqueue(conn, item.id)
    Indexer(library.store, embed, model="fake-embed").run_once()
    chunks = library.client.get_chunks(item.id)
    stored = library.client.create_set(
        EvalSet(
            item_id=item.id,
            item_sha256=item.sha256,
            adapter="fake",
            adapter_version="0",
            samples=[
                Sample(question=f"q{i}", reference_chunk_shas=[c.chunk_sha])
                for i, c in enumerate(chunks)
            ],
        )
    )
    eval_set = library.client.get_set(stored["id"])
    real = library.client._send

    def flaky(method, url, headers, body):
        if body and json.loads(body).get("query") == "q1":
            return 500, b'{"error": "boom"}'
        return real(method, url, headers, body)

    client = LibraryClient(library.client.base_url, "k", transport=flaky)
    run = run_set(client, eval_set, FakeAdapter())
    assert run.status == "partial"
    posted = client.create_run(run)
    assert posted["status"] == "partial"
    assert posted["completed"] == len(chunks) - 1
    assert posted["error_rate"] == pytest.approx(1 / len(chunks))
    assert posted["aggregates"] == {}
    assert posted["partial_aggregates"]["found_at_1"] is not None
    assert {s["name"] for s in posted["suites"]} >= {"id_retrieval", "speed", "freshness"}
    by_q = {s["question"]: s for s in posted["samples"]}
    assert by_q["q1"]["retrieved_chunk_shas"] == []
    assert by_q["q0"]["retrieved_item_ids"] == [item.id] * len(by_q["q0"]["retrieved_chunk_shas"])
    assert by_q["q0"]["latency_ms"] is not None
    assert by_q["q0"]["set_version"] == eval_set.version
    assert by_q["q0"]["freshness"] == "fresh"


def test_create_run_body_of_a_plain_run_is_unchanged():
    from culture_library.eval.models import EvalRun, SampleResult

    sent = []

    def transport(method, url, headers, body):
        sent.append(json.loads(body))
        return 201, b"{}"

    run = EvalRun(set_id=1, samples=[SampleResult(sample_id=2, scores={"a": 1.0})])
    LibraryClient("http://lib", transport=transport).create_run(run)
    assert sent == [run.to_dict()]
