"""The RAGAS 0.4.3 adapter: the only module in culture_library that imports ragas.

Generation is in-house (not ragas's ``TestsetGenerator``) so every sample stays anchored to
exactly one chunk: ``reference_chunk_shas == [chunk.chunk_sha]`` and ``reference_contexts ==
[chunk.text]``. Scoring goes through ragas:

* ``id_based_context_precision`` / ``id_based_context_recall`` — ragas's ID-based metrics,
  fed ``chunk_sha`` as both retrieved and reference context ids (no LLM);
* ``context_precision`` / ``context_recall`` — ragas's LLM context precision/recall *with
  reference*, judged by the OpenAI-compatible endpoint in ``JudgeConfig``
  (``base_url = {judge.url}/v1``). They run only when ``score()`` is given a judge.

Every version-specific ragas call lives in ``_Ragas043``; nothing else here knows ragas's
API. ragas (and openai) are imported lazily, so importing this module never needs the
``[eval]`` extra; constructing ``RagasAdapter`` without one raises ``EvalError``.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from typing import Any, Protocol, Sequence

from .config import JudgeConfig
from .errors import EvalApiError, EvalError
from .models import Chunk, Passage, Sample, ScoreResult

RAGAS_VERSION = "0.4.3"
INSTALL_HINT = "install culture-library[eval] (pins ragas==0.4.3): uv sync --extra eval"

ID_PRECISION = "id_based_context_precision"
ID_RECALL = "id_based_context_recall"
LLM_PRECISION = "context_precision"
LLM_RECALL = "context_recall"
METRICS = (ID_PRECISION, ID_RECALL, LLM_PRECISION, LLM_RECALL)

_GEN_SYSTEM = (
    "You write retrieval-evaluation questions. Given one passage, write ONE question that "
    "can be answered only from that passage, and a short reference answer taken from it. "
    "The question goes to a search engine over a whole library, so it must stand alone: "
    'never refer to "the passage", "the text", "the excerpt" or "the author"; name the '
    "people, places and things it is about instead. "
    'Reply with strict JSON only, exactly: {"question": "...", "answer": "..."}'
)
_GEN_RETRY_JSON = 'That was not valid JSON. Reply with only {"question": "...", "answer": "..."}.'
_GEN_RETRY_SELF = (
    "That question points at its source, which a search engine cannot see. Rewrite it to stand "
    'alone, naming what it is about, and reply with only {"question": "...", "answer": "..."}.'
)
# A question that points at its own source ("the passage", "the author", "this chapter") cannot
# be asked of a search engine. "The text of the Magna Carta" or "the section on tariffs" names
# its subject, so the noun followed by of/on/about/... passes.
_SELF_REFERENCE = re.compile(
    r"\b(?:the|this|that)\s+"
    r"(?:passage|excerpt|extract|text|paragraph|section|chapter|article|story|author|narrator)\b"
    r"(?!\s+(?:of|on|about|in|by|titled|called|named)\b)",
    re.IGNORECASE,
)


class _Backend(Protocol):
    """The ragas calls the adapter needs; ``_Ragas043`` is the real one."""

    def id_precision(self, retrieved_ids: list[str], reference_ids: list[str]) -> float: ...

    def id_recall(self, retrieved_ids: list[str], reference_ids: list[str]) -> float: ...

    async def llm_scores(
        self, items: list[tuple[str, str, list[str]]]
    ) -> list[tuple[float, float]]: ...


class _Ragas043:
    """Every ragas-0.4.3-specific call, in one place.

    Module paths used (ragas 0.4.3):

    * ``ragas.metrics._context_precision.IDBasedContextPrecision`` and
      ``ragas.metrics._context_recall.IDBasedContextRecall`` (imported from their defining
      modules: the ``ragas.metrics`` re-export is deprecated in 0.4.3 and the suggested
      ``ragas.metrics.collections`` has no ID-based metrics) scored with
      ``single_turn_ascore(ragas.dataset_schema.SingleTurnSample(retrieved_context_ids=...,
      reference_context_ids=...))``;
    * ``ragas.metrics.collections.ContextPrecisionWithReference`` and
      ``ragas.metrics.collections.ContextRecall`` scored with ``ascore(user_input=...,
      reference=..., retrieved_contexts=...)`` -> ``MetricResult.value``;
    * ``ragas.llms.base.llm_factory(model, provider="openai", client=AsyncOpenAI(...))``
      (an instructor ``Mode.JSON`` LLM) and ``ragas.embeddings.OpenAIEmbeddings(client,
      model)`` (``ragas.embeddings.openai_provider``), both over ``openai.AsyncOpenAI(
      base_url=f"{judge.url}/v1", api_key=judge.api_key)``.
    """

    def __init__(self, judge: JudgeConfig, embedding_model: str | None = None) -> None:
        # ragas phones home with usage analytics unless told not to; the library never does.
        os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")
        try:
            import openai  # noqa: F401
            import ragas
        except ImportError as exc:
            raise EvalError(f"the ragas adapter needs ragas and openai: {INSTALL_HINT}") from exc
        found = getattr(ragas, "__version__", "?")
        if found != RAGAS_VERSION:
            raise EvalError(
                f"ragas {found} is installed but the adapter is pinned to {RAGAS_VERSION}: "
                f"{INSTALL_HINT}"
            )
        self.judge = judge
        self.embedding_model = embedding_model or judge.model
        self.base_url = judge.url.rstrip("/") + "/v1"

    def components(self) -> dict[str, Any]:
        """A fresh OpenAI-compatible client plus the ragas LLM and embeddings over it.

        Built per scoring pass because an ``AsyncOpenAI`` client is bound to the event loop
        it first runs on.
        """
        from openai import AsyncOpenAI
        from ragas.embeddings import OpenAIEmbeddings
        from ragas.llms.base import llm_factory

        client = AsyncOpenAI(
            base_url=self.base_url, api_key=self.judge.api_key or "unused", max_retries=1
        )
        return {
            "client": client,
            "llm": llm_factory(
                self.judge.model,
                provider="openai",
                client=client,
                max_tokens=self.judge.max_tokens,
            ),
            "embeddings": OpenAIEmbeddings(client=client, model=self.embedding_model),
        }

    def id_precision(self, retrieved_ids: list[str], reference_ids: list[str]) -> float:
        from ragas.metrics._context_precision import IDBasedContextPrecision

        return self._id_metric(IDBasedContextPrecision(), retrieved_ids, reference_ids)

    def id_recall(self, retrieved_ids: list[str], reference_ids: list[str]) -> float:
        from ragas.metrics._context_recall import IDBasedContextRecall

        return self._id_metric(IDBasedContextRecall(), retrieved_ids, reference_ids)

    @staticmethod
    def _id_metric(metric: Any, retrieved_ids: list[str], reference_ids: list[str]) -> float:
        from ragas.dataset_schema import SingleTurnSample

        sample = SingleTurnSample(
            retrieved_context_ids=list(retrieved_ids), reference_context_ids=list(reference_ids)
        )
        return float(asyncio.run(metric.single_turn_ascore(sample)))

    async def llm_scores(
        self, items: list[tuple[str, str, list[str]]]
    ) -> list[tuple[float, float]]:
        from ragas.metrics.collections import ContextPrecisionWithReference, ContextRecall

        parts = self.components()
        try:
            precision = ContextPrecisionWithReference(llm=parts["llm"])
            recall = ContextRecall(llm=parts["llm"])
            out = []
            for question, reference, contexts in items:
                kw = {"user_input": question, "reference": reference}
                p = await precision.ascore(retrieved_contexts=contexts, **kw)
                r = await recall.ascore(retrieved_contexts=contexts, **kw)
                out.append((float(p.value), float(r.value)))
            return out
        finally:
            await parts["client"].close()


def _clean(value: Any) -> float | None:
    if value is None:
        return None
    v = float(value)
    return None if math.isnan(v) else v


def _mean(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _unfence(text: str) -> str:
    """Strip one surrounding ```/```json code fence, if the whole reply is fenced."""
    body = (text or "").strip()
    if body.startswith("```") and body.endswith("```") and len(body) >= 6:
        body = body[3:-3]
        if body.startswith("json"):
            body = body[4:]
    return body.strip()


def _parse_generation(text: str) -> tuple[str, str] | str:
    """``(question, answer)``, or the retry message that says what was wrong."""
    try:
        data = json.loads(_unfence(text))
    except (TypeError, ValueError):
        return _GEN_RETRY_JSON
    if not isinstance(data, dict):
        return _GEN_RETRY_JSON
    q, a = data.get("question"), data.get("answer")
    if not (isinstance(q, str) and q.strip() and isinstance(a, str) and a.strip()):
        return _GEN_RETRY_JSON
    if _SELF_REFERENCE.search(q):
        return _GEN_RETRY_SELF
    return q.strip(), a.strip()


class RagasAdapter:
    """``EvalAdapter`` backed by ragas 0.4.3 (see the module docstring)."""

    name = "ragas"
    version = RAGAS_VERSION

    def __init__(
        self,
        judge: JudgeConfig | None = None,
        *,
        embedding_model: str | None = None,
        backend: _Backend | None = None,
    ) -> None:
        self.judge = judge or JudgeConfig.from_env()
        self.backend = backend or _Ragas043(self.judge, embedding_model)

    # -- generation (in-house, chunk-anchored) --------------------------------------------

    def generate_samples(self, chunks: Sequence[Chunk], n: int, llm) -> list[Sample]:
        """One question + short reference answer per chunk, for the first ``n`` non-header
        chunks. A bad reply (not JSON, or a question that points at "the passage") is retried
        once; a second bad reply skips that chunk."""
        out: list[Sample] = []
        for chunk in [c for c in chunks if not c.is_header][: max(n, 0)]:
            messages = [
                {"role": "system", "content": _GEN_SYSTEM},
                {"role": "user", "content": f"Passage:\n{chunk.text}"},
            ]
            reply = llm.complete(messages)
            parsed = _parse_generation(reply)
            if isinstance(parsed, str):
                retry = messages + [
                    {"role": "assistant", "content": reply},
                    {"role": "user", "content": parsed},
                ]
                parsed = _parse_generation(llm.complete(retry))
            if isinstance(parsed, str):
                continue
            question, answer = parsed
            out.append(
                Sample(
                    question=question,
                    reference_answer=answer,
                    reference_chunk_shas=[chunk.chunk_sha],
                    reference_contexts=[chunk.text],
                )
            )
        return out

    # -- scoring (ragas) -------------------------------------------------------------------

    def score(
        self,
        samples: Sequence[Sample],
        retrieved: Sequence[Sequence[Passage]],
        llm,
    ) -> ScoreResult:
        """Score each sample's retrieved passages.

        ID-based metrics always run. LLM metrics run only when ``llm`` is not None (a judge
        is configured); ragas reaches it through ``JudgeConfig``, not through ``llm``. A
        metric that cannot be computed (no retrieved passages, no reference) is ``None``.
        """
        if len(samples) != len(retrieved):
            raise EvalError(
                f"score() got {len(samples)} samples but {len(retrieved)} retrieval results"
            )
        per = [self._id_row(s, ps) for s, ps in zip(samples, retrieved)]
        judge_items: list[tuple[str, str, list[str]]] = []
        judge_rows: list[int] = []
        for i, (sample, passages) in enumerate(zip(samples, retrieved)):
            contexts = [p.text for p in passages if p.text]
            if llm is not None and contexts and sample.question and sample.reference_answer:
                judge_items.append((sample.question, sample.reference_answer, contexts))
                judge_rows.append(i)
        if judge_items:
            self._apply_llm_scores(per, judge_items, judge_rows)
        aggregate = {m: _mean([row[m] for row in per]) for m in METRICS}
        return ScoreResult(per_sample=per, aggregate=aggregate)

    def _id_row(self, sample: Sample, passages: Sequence[Passage]) -> dict[str, float | None]:
        """ID-based precision/recall over chunk_sha; None when undefined."""
        got = [p.chunk_sha for p in passages]
        ref = list(sample.reference_chunk_shas)
        row: dict[str, float | None] = dict.fromkeys(METRICS)
        if ref:
            row[ID_PRECISION] = _clean(self.backend.id_precision(got, ref)) if got else None
            row[ID_RECALL] = _clean(self.backend.id_recall(got, ref))
        return row

    def _apply_llm_scores(
        self,
        per: list[dict[str, float | None]],
        judge_items: list[tuple[str, str, list[str]]],
        judge_rows: list[int],
    ) -> None:
        try:
            scores = asyncio.run(self.backend.llm_scores(judge_items))
        except EvalError:
            raise
        except Exception as exc:  # ragas / openai / instructor failures
            raise EvalApiError(f"ragas LLM metrics failed: {exc}") from exc
        for i, (p, r) in zip(judge_rows, scores):
            per[i][LLM_PRECISION] = _clean(p)
            per[i][LLM_RECALL] = _clean(r)
