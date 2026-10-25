"""t13: eval/runner.py — run a set against /api/rag/retrieve and score it."""

from __future__ import annotations

import json

import pytest

from culture_library.eval.client import LibraryClient
from culture_library.eval.config import JudgeConfig, OpenAIChat
from culture_library.eval.models import EvalSet, Sample, ScoreResult
from culture_library.eval.runner import EvalStaleError, run_set

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


def test_refuses_when_item_sha_changed(lib):
    lib.chunks_info["item_sha256"] = "c" * 64
    with pytest.raises(EvalStaleError) as ei:
        run_set(lib.client, _set(Sample(question="q")), FakeAdapter())
    assert ei.value.hint
    assert lib.retrieve_bodies == []


def test_refuses_when_item_archived_or_missing(lib):
    del lib.items[1]
    with pytest.raises(EvalStaleError) as ei:
        run_set(lib.client, _set(Sample(question="q")), FakeAdapter())
    assert "archived" in str(ei.value)
    assert ei.value.hint


def test_refuses_when_index_not_done(lib):
    lib.chunks_info["index_state"] = "pending"
    with pytest.raises(EvalStaleError):
        run_set(lib.client, _set(Sample(question="q")), FakeAdapter())
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
    lib.passages = {"q0": [_p("s")], "q1": [_p("s")]}
    lib.fail_on = {"q2"}
    samples = [Sample(question=f"q{i}", reference_chunk_shas=["s"]) for i in range(3)]
    run = run_set(lib.client, _set(*samples), FakeAdapter())
    assert run.status == "partial"
    assert run.completed == 2
    assert run.total == 3
    assert run.aggregates is None
    assert len(run.samples) == 2
    assert run.to_dict()["aggregates"] is None


def test_failure_on_first_sample_is_failed(lib):
    lib.fail_on = {"q0"}
    run = run_set(lib.client, _set(Sample(question="q0"), Sample(question="q1")), FakeAdapter())
    assert run.status == "failed"
    assert run.completed == 0
    assert run.aggregates is None
    assert run.samples == []


def test_adapter_failure_drops_aggregates(lib, fake_judge_server):
    class Boom(FakeAdapter):
        def score(self, samples, retrieved, llm):
            raise ValueError("judge said nonsense")

    judge = fake_judge_server.config()
    run = run_set(
        lib.client, _set(Sample(question="q")), Boom(), llm=OpenAIChat(judge), judge=judge
    )
    assert run.status == "partial"
    assert run.aggregates is None


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
