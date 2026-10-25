"""Run an eval set against ``POST /api/rag/retrieve`` and score it.

ID-based context precision/recall over ``chunk_sha`` are computed here, so a run scores
without ragas; LLM metrics come only from the adapter, and only when a judge is configured.
The returned :class:`EvalRun` is *not* posted (the CLI does that).
"""

from __future__ import annotations

import dataclasses
from typing import Any, Sequence

from .adapter import ChatLLM, EvalAdapter
from .client import LibraryClient
from .config import JudgeConfig
from .errors import EvalApiError, EvalError
from .models import EvalRun, EvalSet, Passage, SampleResult

PRECISION = "id_based_context_precision"
RECALL = "id_based_context_recall"
_ID_KEYS = (PRECISION, RECALL)


class EvalStaleError(EvalError):
    """The set no longer matches its item (bytes changed, archived/missing, or not indexed)."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


class IdOnlyAdapter:
    """Adapter for ID-metric-only runs (``--no-llm``): needs no eval extra, never scores."""

    name = "id-only"
    version = "1"

    def generate_samples(self, chunks, n, llm):
        raise EvalError("the id-only adapter does not generate samples")

    def score(self, samples, retrieved, llm):
        raise EvalError("the id-only adapter does not score with an LLM")


def id_metrics(retrieved: Sequence[str], reference: Sequence[str]) -> dict[str, float | None]:
    """Precision = |retrieved ∩ reference| / |retrieved|; recall = ... / |reference|."""
    got, ref = set(retrieved), set(reference)
    hits = len(got & ref)
    return {
        PRECISION: hits / len(got) if got else None,
        RECALL: hits / len(ref) if ref else None,
    }


def precheck(client: LibraryClient, eval_set: EvalSet) -> dict:
    """Refuse a run whose item changed, was archived, or is not fully indexed."""
    iid = eval_set.item_id
    try:
        item = client.get_item(iid)
    except EvalApiError as exc:
        if exc.status == 404:
            raise EvalStaleError(
                f"item {iid} is archived or missing",
                hint="restore the item, or generate a new set for a live item",
            ) from exc
        raise
    if item.get("archived_at"):
        raise EvalStaleError(f"item {iid} is archived", hint="restore the item first")
    info = client.request("GET", f"/api/rag/items/{int(iid)}/chunks") or {}
    sha = info.get("item_sha256") or item.get("sha256")
    if sha != eval_set.item_sha256:
        raise EvalStaleError(
            f"item {iid} changed since set {eval_set.id} was generated "
            f"({eval_set.item_sha256} -> {sha})",
            hint="regenerate the eval set against the item's current bytes",
        )
    state = info.get("index_state")
    if state != "done":
        raise EvalStaleError(
            f"item {iid} index is {state!r}, not 'done'",
            hint="wait for the indexer to finish (see /api/status) and retry",
        )
    return item


def _mean(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


class _Visibility:
    """Per-item visibility lookups, cached; unknown/unreadable counts as non-public."""

    def __init__(self, client: LibraryClient, seed: dict[int, str] | None = None):
        self._client = client
        self._cache: dict[int, str | None] = dict(seed or {})

    def public(self, item_id: int) -> bool:
        if item_id not in self._cache:
            try:
                self._cache[item_id] = self._client.get_item(item_id).get("visibility")
            except EvalApiError:
                self._cache[item_id] = None
        return self._cache[item_id] == "public"


def run_set(
    client: LibraryClient,
    eval_set: EvalSet,
    adapter: EvalAdapter,
    *,
    llm: ChatLLM | None = None,
    judge: JudgeConfig | None = None,
    mode: str = "hybrid",
    k: int = 5,
    rerank: bool | None = None,
    candidates: int | None = None,
) -> EvalRun:
    """Retrieve every sample's question, score it, and return the (unposted) run.

    Raises :class:`EvalStaleError` (no run recorded) when the pre-check fails.
    """
    item = precheck(client, eval_set)
    samples = list(eval_set.samples)
    config: dict[str, Any] = {
        "mode": mode,
        "k": k,
        "rerank": rerank,
        "candidates": candidates,
        "adapter": {"name": adapter.name, "version": adapter.version},
        "judge_model": judge.model if judge is not None else None,
        "server": (client.status() or {}).get("retrieval", {}),
    }
    run = EvalRun(set_id=eval_set.id, config=config, total=len(samples), completed=0)

    retrieved: list[list[Passage]] = []
    for i, sample in enumerate(samples):
        try:
            passages = client.retrieve(
                sample.question, k=k, mode=mode, rerank=rerank, candidates=candidates
            )
        except (EvalError, KeyError, TypeError, ValueError) as exc:
            return _fail(run, i, exc)
        shas = [p.chunk_sha for p in passages]
        retrieved.append(passages)
        run.samples.append(
            SampleResult(
                sample_id=sample.id,
                retrieved_chunk_shas=shas,
                scores=id_metrics(shas, sample.reference_chunk_shas),
            )
        )
        run.completed = i + 1

    if llm is not None and judge is not None:
        try:
            _llm_scores(
                client, run, eval_set.item_id, item, samples, retrieved, adapter, llm, judge
            )
        except (EvalError, KeyError, TypeError, ValueError) as exc:
            return _fail(run, run.completed, exc, keep_samples=True)

    run.status = "complete"
    aggregates: dict[str, float | None] = {
        PRECISION: _mean([r.scores.get(PRECISION) for r in run.samples]),
        RECALL: _mean([r.scores.get(RECALL) for r in run.samples]),
    }
    aggregates.update({k: v for k, v in (run.aggregates or {}).items() if k not in _ID_KEYS})
    run.aggregates = aggregates
    return run


def _llm_scores(client, run, item_id, item, samples, retrieved, adapter, llm, judge) -> None:
    if not judge.is_local and item.get("visibility") != "public":
        # The questions and references are derived from this item's text.
        config = run.config
        config["llm_metrics_skipped"] = (
            f"item {item_id} is not public and judge {judge.url!r} is not local"
        )
        return
    if not judge.is_local:
        vis = _Visibility(client, {int(item_id): item.get("visibility")})
        retrieved = [
            [p if vis.public(p.item_id) else dataclasses.replace(p, text="") for p in ps]
            for ps in retrieved
        ]
    result = adapter.score(samples, retrieved, llm)
    for res, extra in zip(run.samples, result.per_sample):
        res.scores.update({k: v for k, v in extra.items() if k not in _ID_KEYS})
    run.aggregates = {k: v for k, v in result.aggregate.items() if k not in _ID_KEYS}


def _fail(run: EvalRun, i: int, exc: Exception, *, keep_samples: bool = False) -> EvalRun:
    run.status = "partial" if i > 0 else "failed"
    run.completed = i
    run.aggregates = None
    run.config["error"] = str(exc)
    if not keep_samples:
        run.samples = run.samples[:i]
    return run


__all__ = ["EvalStaleError", "id_metrics", "precheck", "run_set"]
