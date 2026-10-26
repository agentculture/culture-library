"""RagasAdapter (t14): lazy ragas import, in-house chunk-anchored generation, ragas scoring.

Tests without the ``ragas`` marker run everywhere (ragas calls stubbed through the adapter's
backend seam). ``@pytest.mark.ragas`` tests drive real ragas 0.4.3 against the conftest
``fake_judge_server`` and need ``uv sync --extra eval``.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from culture_library.eval.adapter import EvalAdapter
from culture_library.eval.config import JudgeConfig
from culture_library.eval.errors import EvalApiError, EvalError
from culture_library.eval.models import Chunk, Passage, Sample

ROOT = Path(__file__).resolve().parents[2]

# One reply that parses as every structured output the adapter asks for: the generator's
# {"question", "answer"} and ragas 0.4.3's ContextPrecisionOutput / ContextRecallOutput.
UNIVERSAL_REPLY = json.dumps(
    {
        "question": "What colour is the lamp in chunk one?",
        "answer": "The lamp is green.",
        "reason": "The context states it directly.",
        "verdict": 1,
        "classifications": [
            {"statement": "The lamp is green.", "reason": "Stated.", "attributed": 1}
        ],
    }
)


def _chunks() -> list[Chunk]:
    return [
        Chunk(chunk_no=0, chunk_sha="h0", text="# Title", is_header=True),
        Chunk(chunk_no=1, chunk_sha="s1", text="The lamp in the hall is green."),
        Chunk(chunk_no=2, chunk_sha="s2", text="The door at the back is red."),
        Chunk(chunk_no=3, chunk_sha="s3", text="The cat sleeps on the mat."),
    ]


def _in_house_id_metrics(sample: Sample, passages: list[Passage]) -> tuple[float, float]:
    """The plan's t13 definition: precision = relevant retrieved / retrieved,
    recall = reference shas retrieved / reference shas."""
    got = [p.chunk_sha for p in passages]
    ref = set(sample.reference_chunk_shas)
    precision = sum(1 for s in got if s in ref) / len(got)
    recall = sum(1 for s in ref if s in set(got)) / len(ref)
    return precision, recall


def _two_sample_set():
    samples = [
        Sample(
            question="What colour is the lamp?",
            reference_answer="Green.",
            reference_chunk_shas=["s1"],
            reference_contexts=["The lamp in the hall is green."],
        ),
        Sample(
            question="What colour is the door?",
            reference_answer="Red.",
            reference_chunk_shas=["s2", "s3"],
            reference_contexts=["The door at the back is red.", "The cat sleeps on the mat."],
        ),
    ]
    retrieved = [
        [
            Passage(item_id=1, chunk_sha="s1", text="The lamp in the hall is green."),
            Passage(item_id=1, chunk_sha="s3", text="The cat sleeps on the mat."),
        ],
        [
            Passage(item_id=1, chunk_sha="s9", text="Unrelated text."),
            Passage(item_id=1, chunk_sha="s2", text="The door at the back is red."),
            Passage(item_id=1, chunk_sha="s8", text="More unrelated text."),
        ],
    ]
    return samples, retrieved


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def complete(self, messages):
        self.calls.append(messages)
        return self.replies.pop(0)


class StubBackend:
    """Stands in for the version-pinned ragas calls; records what it was asked."""

    def __init__(self, llm_value=0.5):
        self.llm_value = llm_value
        self.calls: list[tuple] = []

    def id_precision(self, retrieved_ids, reference_ids):
        self.calls.append(("id_precision", list(retrieved_ids), list(reference_ids)))
        if not retrieved_ids:
            return float("nan")
        ref = set(reference_ids)
        got = set(retrieved_ids)
        return sum(1 for r in got if r in ref) / len(got)

    def id_recall(self, retrieved_ids, reference_ids):
        self.calls.append(("id_recall", list(retrieved_ids), list(reference_ids)))
        ref = set(reference_ids)
        return sum(1 for r in ref if r in set(retrieved_ids)) / len(ref)

    async def llm_scores(self, items):
        self.calls.append(("llm_scores", items))
        return [(self.llm_value, self.llm_value) for _ in items]


# --- lazy import ------------------------------------------------------------------------


def test_module_imports_without_ragas_and_constructor_hints_install():
    code = (
        "import sys\n"
        "class B:\n"
        "    def find_spec(self, n, p=None, t=None):\n"
        "        if n.split('.')[0] in {'ragas','openai','instructor','langchain_core'}:\n"
        "            raise ImportError(n)\n"
        "sys.meta_path.insert(0, B())\n"
        "import culture_library.eval.ragas_adapter as ra\n"
        "from culture_library.eval.errors import EvalError\n"
        "assert 'ragas' not in sys.modules\n"
        "try:\n"
        "    ra.RagasAdapter()\n"
        "except EvalError as e:\n"
        "    assert 'install culture-library[eval]' in str(e), str(e)\n"
        "    print('OK')\n"
    )
    r = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "OK"


def test_adapter_satisfies_protocol_with_injected_backend():
    from culture_library.eval.ragas_adapter import RagasAdapter

    a = RagasAdapter(backend=StubBackend())
    assert isinstance(a, EvalAdapter)
    assert a.name == "ragas"
    assert a.version == "0.4.3"


# --- generation (in-house, chunk-anchored) ---------------------------------------------


def test_generate_samples_anchors_each_sample_to_its_chunk():
    from culture_library.eval.ragas_adapter import RagasAdapter

    llm = ScriptedLLM(
        [
            '{"question": "What colour is the lamp?", "answer": "Green."}',
            '```json\n{"question": "What colour is the door?", "answer": "Red."}\n```',
        ]
    )
    out = RagasAdapter(backend=StubBackend()).generate_samples(_chunks(), 2, llm)
    assert [s.question for s in out] == ["What colour is the lamp?", "What colour is the door?"]
    assert [s.reference_answer for s in out] == ["Green.", "Red."]
    assert [s.reference_chunk_shas for s in out] == [["s1"], ["s2"]]
    assert [s.reference_contexts for s in out] == [
        ["The lamp in the hall is green."],
        ["The door at the back is red."],
    ]
    # the header chunk is never sent; each prompt carries exactly its chunk's text
    assert len(llm.calls) == 2
    assert "The lamp in the hall is green." in json.dumps(llm.calls[0])
    assert "# Title" not in json.dumps(llm.calls)


def test_generate_samples_retries_bad_json_once_then_skips():
    from culture_library.eval.ragas_adapter import RagasAdapter

    llm = ScriptedLLM(
        [
            "not json",  # chunk s1, try 1
            '{"question": "Q1?", "answer": "A1"}',  # chunk s1, retry -> ok
            "still not json",  # chunk s2, try 1
            '{"question": ""}',  # chunk s2, retry -> invalid -> skip
            '{"question": "Q3?", "answer": "A3"}',  # chunk s3
        ]
    )
    out = RagasAdapter(backend=StubBackend()).generate_samples(_chunks(), 3, llm)
    assert [(s.question, s.reference_chunk_shas) for s in out] == [
        ("Q1?", ["s1"]),
        ("Q3?", ["s3"]),
    ]
    assert len(llm.calls) == 5


def test_generate_samples_retries_a_question_that_points_at_its_source():
    from culture_library.eval.ragas_adapter import RagasAdapter

    llm = ScriptedLLM(
        [
            '{"question": "According to the passage, who wins?", "answer": "A1"}',
            '{"question": "Who wins the race in Kent?", "answer": "A1"}',  # retry -> ok
            '{"question": "Which door is red?", "answer": "A2"}',
            '{"question": "Why does the author mention the cat?", "answer": "A3"}',
            '{"question": "What is the main point of this excerpt?", "answer": "A3"}',  # skip
        ]
    )
    out = RagasAdapter(backend=StubBackend()).generate_samples(_chunks(), 3, llm)
    assert [s.question for s in out] == ["Who wins the race in Kent?", "Which door is red?"]
    assert "stand alone" in llm.calls[1][-1]["content"]


def test_retry_message_names_the_problem():
    from culture_library.eval.ragas_adapter import RagasAdapter

    llm = ScriptedLLM(["not json", '{"question": "Q1?", "answer": "A1"}'])
    RagasAdapter(backend=StubBackend()).generate_samples(_chunks(), 1, llm)
    assert "not valid JSON" in llm.calls[1][-1]["content"]


@pytest.mark.parametrize(
    "question, bad",
    [
        ("What does the passage say about lamps?", True),
        ("According to the article, who resigned?", True),
        ("In this chapter, who arrives?", True),
        ("What is the moral of the story?", True),
        ("What right is guaranteed in the text of the Magna Carta?", False),
        ("What does Article 5 say in the section on tariffs?", False),
        ("Who is the author of Erewhon?", False),
        ("Which door at the back of the hall is red?", False),
    ],
)
def test_self_reference_check(question, bad):
    from culture_library.eval.ragas_adapter import _SELF_REFERENCE

    assert bool(_SELF_REFERENCE.search(question)) is bad


def test_generate_samples_propagates_judge_errors():
    from culture_library.eval.ragas_adapter import RagasAdapter

    class Boom:
        def complete(self, messages):
            raise EvalApiError("judge down")

    adapter = RagasAdapter(backend=StubBackend())
    chunks = _chunks()
    judge = Boom()
    with pytest.raises(EvalApiError):
        adapter.generate_samples(chunks, 1, judge)


# --- scoring (ragas calls stubbed) -----------------------------------------------------


def test_score_feeds_chunk_shas_to_id_metrics_and_matches_in_house():
    from culture_library.eval.ragas_adapter import RagasAdapter

    samples, retrieved = _two_sample_set()
    backend = StubBackend()
    res = RagasAdapter(backend=backend).score(samples, retrieved, llm=None)
    assert ("id_precision", ["s1", "s3"], ["s1"]) in backend.calls
    for s, ps, row in zip(samples, retrieved, res.per_sample):
        p, r = _in_house_id_metrics(s, ps)
        assert row["id_based_context_precision"] == pytest.approx(p)
        assert row["id_based_context_recall"] == pytest.approx(r)
        # no judge -> LLM metrics are recorded as missing, not invented
        assert row["context_precision"] is None
        assert row["context_recall"] is None
    assert not any(c[0] == "llm_scores" for c in backend.calls)
    assert res.aggregate["id_based_context_precision"] == pytest.approx((0.5 + 1 / 3) / 2)
    assert res.aggregate["id_based_context_recall"] == pytest.approx((1.0 + 0.5) / 2)
    assert res.aggregate["context_precision"] is None


def test_score_with_judge_runs_llm_metrics_with_reference():
    from culture_library.eval.ragas_adapter import RagasAdapter

    samples, retrieved = _two_sample_set()
    backend = StubBackend(llm_value=0.25)
    res = RagasAdapter(backend=backend).score(samples, retrieved, llm=object())
    (call,) = [c for c in backend.calls if c[0] == "llm_scores"]
    items = call[1]
    assert items[0] == (
        "What colour is the lamp?",
        "Green.",
        ["The lamp in the hall is green.", "The cat sleeps on the mat."],
    )
    assert res.per_sample[1]["context_precision"] == 0.25
    assert res.aggregate["context_recall"] == 0.25


def test_score_empty_retrieval_is_none_not_nan():
    from culture_library.eval.ragas_adapter import RagasAdapter

    samples, _ = _two_sample_set()
    res = RagasAdapter(backend=StubBackend()).score(samples[:1], [[]], llm=object())
    row = res.per_sample[0]
    assert row["id_based_context_precision"] is None
    assert row["id_based_context_recall"] == 0.0
    assert row["context_precision"] is None  # nothing to judge
    assert all(v is None or not math.isnan(v) for v in res.aggregate.values())


def test_score_length_mismatch_is_an_error():
    from culture_library.eval.ragas_adapter import RagasAdapter

    samples, retrieved = _two_sample_set()
    adapter = RagasAdapter(backend=StubBackend())
    short = retrieved[:1]
    with pytest.raises(EvalError):
        adapter.score(samples, short, llm=None)


# --- real ragas 0.4.3 ------------------------------------------------------------------


@pytest.mark.ragas
def test_ragas_two_sample_set_against_fake_openai_server(fake_judge_server):
    pytest.importorskip("ragas")
    from culture_library.eval.ragas_adapter import RagasAdapter

    fake_judge_server.reply = UNIVERSAL_REPLY
    judge = fake_judge_server.config(model="cortex", api_key="k")
    adapter = RagasAdapter(judge)

    parts = adapter.backend.components()
    assert str(parts["client"].base_url).rstrip("/") == f"{judge.url}/v1"
    assert parts["embeddings"].client is parts["client"]
    assert parts["llm"].model == "cortex"

    samples, retrieved = _two_sample_set()
    res = adapter.score(samples, retrieved, llm=object())

    # ID metrics computed by ragas match the in-house definition
    for s, ps, row in zip(samples, retrieved, res.per_sample):
        p, r = _in_house_id_metrics(s, ps)
        assert row["id_based_context_precision"] == pytest.approx(p)
        assert row["id_based_context_recall"] == pytest.approx(r)
        # every verdict / attribution in the fake reply is 1 -> LLM metrics are 1.0
        assert row["context_precision"] == pytest.approx(1.0)
        assert row["context_recall"] == pytest.approx(1.0)

    # ragas talked to the fake OpenAI-compatible server with the judge's model + key
    assert fake_judge_server.payloads, "ragas never called the judge"
    assert {p["model"] for p in fake_judge_server.payloads} == {"cortex"}
    assert set(fake_judge_server.auth_headers) == {"Bearer k"}
    # precision: one call per retrieved context (2 + 3); recall: one call per sample (2)
    assert len(fake_judge_server.payloads) == 7
    sent = json.dumps(fake_judge_server.payloads)
    assert "The door at the back is red." in sent
    assert "Green." in sent


@pytest.mark.ragas
def test_ragas_generate_samples_via_openai_chat(fake_judge_server):
    pytest.importorskip("ragas")
    from culture_library.eval.config import OpenAIChat
    from culture_library.eval.ragas_adapter import RagasAdapter

    fake_judge_server.reply = UNIVERSAL_REPLY
    judge = fake_judge_server.config()
    out = RagasAdapter(judge).generate_samples(_chunks(), 2, OpenAIChat(judge))
    assert [s.reference_chunk_shas for s in out] == [["s1"], ["s2"]]
    assert out[0].question == "What colour is the lamp in chunk one?"


@pytest.mark.ragas
def test_real_backend_passes_the_judge_output_limit(monkeypatch):
    pytest.importorskip("ragas")
    import ragas.llms.base as base

    from culture_library.eval.ragas_adapter import _Ragas043

    seen = {}

    def fake_factory(model, **kw):
        seen.update(kw, model=model)
        return object()

    monkeypatch.setattr(base, "llm_factory", fake_factory)
    judge = JudgeConfig(url="http://127.0.0.1:9", model="cortex", max_tokens=12345)
    _Ragas043(judge).components()
    assert seen["max_tokens"] == 12345
    assert seen["model"] == "cortex"
