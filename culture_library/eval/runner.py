"""Run an eval set against ``POST /api/rag/retrieve`` and score it with the suite registry.

Every question is retrieved once (timed, client side); a failed retrieval is recorded as an
error for that question with an empty retrieval and the run keeps going. The requested suites
(default: every judge-free suite, plus ``judged`` when a judge is configured) then score the
results. Suites that score *retrieved results* (``id_retrieval``, ``result_quality``,
``judged``, and any added suite) see only the successful questions; ``speed`` and
``freshness``, which describe every question, see them all. The legacy ID context
precision/recall over ``chunk_sha`` are still computed here, so their keys stay owned by the
runner. The returned :class:`SuiteRun` is *not* posted (the CLI does that).

Status: every retrieval succeeded -> ``complete`` with ``aggregates``; some failed ->
``partial`` with ``partial_aggregates`` (over the successes), ``completed`` = successes; none
succeeded -> ``failed`` with neither. ``error_rate`` is failures / questions.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

from . import suites as registry
from .adapter import ChatLLM, EvalAdapter
from .config import JudgeConfig
from .errors import EvalApiError, EvalError
from .models import EvalRun, EvalSet, Passage, SampleResult, ScoreResult
from .suites import SuiteContext, SuiteScore
from .suites import freshness as freshness_suite
from .suites.judged import JudgedSuite

log = logging.getLogger(__name__)

PRECISION = "id_based_context_precision"
RECALL = "id_based_context_recall"
_ID_KEYS = (PRECISION, RECALL)
JUDGED = "judged"
# Suites that describe every question (latency/errors, reference freshness) rather than the
# retrieved results; they score failed questions too.
ALL_QUESTION_SUITES = frozenset({"speed", "freshness"})
_RETRIEVAL_ERRORS = (EvalError, KeyError, TypeError, ValueError)
_FRESHNESS_STATE = {
    "fresh_count": freshness_suite.FRESH,
    "stale_count": freshness_suite.STALE,
    "fresh_unknown_count": freshness_suite.UNKNOWN,
    "fresh_na_count": freshness_suite.NA,
}


@dataclass
class SuiteSampleResult(SampleResult):
    """A scored question plus the run's own snapshot of what it asked and got."""

    question: str | None = None
    reference_chunk_shas: list[str] | None = None
    set_version: int | None = None
    retrieved_item_ids: list[int] | None = None  # one per rank, aligned with the chunk shas
    latency_ms: float | None = None  # None when the retrieval failed
    freshness: str | None = None  # fresh | stale | unknown | not_applicable


class RetrievalClient(Protocol):
    """The client methods the runner calls: ``LibraryClient`` over HTTP, or the server's
    in-process client."""

    def status(self) -> dict: ...

    def get_item(self, item_id: int) -> dict: ...

    def request(self, method: str, path: str) -> Any: ...

    def retrieve(
        self, query: str, k: int = 5, mode: str = "hybrid", **opts: Any
    ) -> list[Passage]: ...


@dataclass
class SuiteRun(EvalRun):
    """An :class:`EvalRun` with the suite runner's fields (posted by ``create_run``)."""

    partial_aggregates: dict[str, float | None] | None = None
    error_rate: float | None = None
    suites: list[dict[str, Any]] = field(default_factory=list)


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


def _precheck(client: RetrievalClient, eval_set: EvalSet) -> tuple[dict, str | None]:
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
    state = info.get("index_state")
    if state != "done":
        raise EvalStaleError(
            f"item {iid} index is {state!r}, not 'done'",
            hint="wait for the indexer to finish (see /api/status) and retry",
        )
    return item, info.get("item_sha256") or item.get("sha256")


def precheck(client: RetrievalClient, eval_set: EvalSet) -> dict:
    """Refuse a run whose item was archived/removed or is not fully indexed.

    A changed item (bytes differ from the set's ``item_sha256``) no longer refuses: the run
    goes ahead and the freshness suite reports which questions' references are gone.
    """
    return _precheck(client, eval_set)[0]


def _mean(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


class _Visibility:
    """Per-item visibility lookups, cached; unknown/unreadable counts as non-public."""

    def __init__(self, client: RetrievalClient, seed: dict[int, str] | None = None):
        self._client = client
        self._cache: dict[int, str | None] = dict(seed or {})

    def public(self, item_id: int) -> bool:
        if item_id not in self._cache:
            try:
                self._cache[item_id] = self._client.get_item(item_id).get("visibility")
            except EvalApiError:
                self._cache[item_id] = None
        return self._cache[item_id] == "public"


class _CapturingAdapter:
    """Hands the caller's adapter to the judged suite and keeps its raw result, so metrics
    the adapter reports beyond the judged suite's own keys still reach the run."""

    def __init__(self, adapter: EvalAdapter):
        self.adapter = adapter
        self.result: ScoreResult | None = None

    def score(self, samples, retrieved, llm) -> ScoreResult:
        self.result = self.adapter.score(samples, retrieved, llm)
        return self.result


def default_suites(*, judge: JudgeConfig | None = None, llm: ChatLLM | None = None) -> list[str]:
    """Every registered judge-free suite, plus ``judged`` when a judge and LLM are given."""
    names = [s.name for s in registry.all_suites() if not s.needs_judge]
    if judge is not None and llm is not None and JUDGED in registry.names():
        names.append(JUDGED)
    return names


def _resolve(names: Sequence[str]) -> list:
    out = []
    for name in dict.fromkeys(names):
        try:
            out.append(registry.get(name))
        except KeyError as exc:
            raise EvalError(exc.args[0]) from None
    return out


# Called once per question as its retrieval finishes (never for the warmup call): the
# question's index in the set, the sample, its passages (None when it failed), the latency in
# ms (None when it failed) and the error string (None when it succeeded).
OnRetrieval = Callable[[int, Any, "list[Passage] | None", "float | None", "str | None"], None]


@dataclass
class _Retrieval:
    passages: list[Passage]
    latency_ms: float


def _retrieve_one(client, question: str, opts: dict) -> tuple[_Retrieval | None, str | None]:
    """One timed retrieval: (outcome, None) on success, (None, error string) on failure."""
    start = time.perf_counter()
    try:
        passages = client.retrieve(question, **opts)
    except _RETRIEVAL_ERRORS as exc:
        return None, str(exc) or type(exc).__name__
    return _Retrieval(passages, (time.perf_counter() - start) * 1000.0), None


def _retrieve_all(client, samples, *, k, mode, rerank, candidates, warmup, on_retrieval=None):
    """Retrieve every question; a failure is that question's error, never the run's end.

    Each call is timed client side with ``time.perf_counter`` (HTTP round trip plus JSON
    decode). ``warmup`` first makes one un-timed, unrecorded call so the first timed one is
    not cold. ``on_retrieval`` (see :data:`OnRetrieval`) reports each question as it finishes;
    an exception it raises is logged and the run goes on.
    """
    opts = {"k": k, "mode": mode, "rerank": rerank, "candidates": candidates}
    if warmup and samples:
        try:
            client.retrieve(samples[0].question, **opts)
        except _RETRIEVAL_ERRORS:
            pass
    outcomes: list[_Retrieval | None] = []
    errors: list[str | None] = []
    for index, sample in enumerate(samples):
        done, error = _retrieve_one(client, sample.question, opts)
        outcomes.append(done)
        errors.append(error)
        if on_retrieval is not None:
            _report(on_retrieval, index, sample, done, error)
    return outcomes, errors


def _report(on_retrieval: OnRetrieval, index: int, sample, done, error) -> None:
    """Call ``on_retrieval``; its failure is logged, never the run's end (it only reports)."""
    try:
        on_retrieval(
            index,
            sample,
            done.passages if done else None,
            done.latency_ms if done else None,
            error,
        )
    except Exception:  # a progress reporter must not abort scoring
        log.warning("on_retrieval failed for question %d", index, exc_info=True)


def _build_run(
    eval_set: EvalSet,
    samples: list,
    config: dict[str, Any],
    outcomes: list,
    errors: list,
) -> SuiteRun:
    """The unscored run: one result per sample with its retrieval and ID metrics."""
    retrieved: list[list[Passage]] = [o.passages if o else [] for o in outcomes]
    n_failed = sum(1 for e in errors if e)
    run = SuiteRun(
        set_id=eval_set.id,
        config=config,
        total=len(samples),
        completed=sum(1 for o in outcomes if o is not None),
        error_rate=(n_failed / len(samples)) if samples else None,
    )
    for i, sample in enumerate(samples):
        shas = [p.chunk_sha for p in retrieved[i]]
        run.samples.append(
            SuiteSampleResult(
                sample_id=sample.id,
                retrieved_chunk_shas=shas,
                scores=(
                    id_metrics(shas, sample.reference_chunk_shas) if outcomes[i] is not None else {}
                ),
                question=sample.question,
                reference_chunk_shas=list(sample.reference_chunk_shas),
                set_version=eval_set.version,
                retrieved_item_ids=[p.item_id for p in retrieved[i]],
                latency_ms=round(outcomes[i].latency_ms, 3) if outcomes[i] else None,
            )
        )
    return run


def _merge_score(
    run: SuiteRun, suite, score: SuiteScore, rows: list[int], aggregates: dict[str, float | None]
) -> None:
    """Fold one suite's per-sample scores and aggregates into the run."""
    for i, row in zip(rows, score.per_sample):
        run.samples[i].scores.update({k_: v for k_, v in row.items() if k_ not in _ID_KEYS})
    aggregates.update({k_: v for k_, v in score.aggregate.items() if k_ not in _ID_KEYS})
    run.suites.append({"name": suite.name, "version": suite.version})
    if suite.name == freshness_suite.SUITE.name:
        for res, row in zip(run.samples, score.per_sample):
            res.freshness = next(
                (st for key, st in _FRESHNESS_STATE.items() if (row.get(key) or 0) >= 1.0), None
            )


def run_set(
    client: RetrievalClient,
    eval_set: EvalSet,
    adapter: EvalAdapter,
    *,
    llm: ChatLLM | None = None,
    judge: JudgeConfig | None = None,
    mode: str = "hybrid",
    k: int = 5,
    rerank: bool | None = None,
    candidates: int | None = None,
    suites: Sequence[str] | None = None,
    warmup: bool = False,
    on_retrieval: OnRetrieval | None = None,
) -> SuiteRun:
    """Retrieve every sample's question, score it with ``suites``, return the unposted run.

    ``suites`` defaults to :func:`default_suites`. Raises :class:`EvalStaleError` (no run
    recorded) when the item is archived, missing or not fully indexed, and :class:`EvalError`
    for an unknown suite name.
    """
    chosen = _resolve(default_suites(judge=judge, llm=llm) if suites is None else suites)
    item, current_sha = _precheck(client, eval_set)
    samples = list(eval_set.samples)
    config: dict[str, Any] = {
        "mode": mode,
        "k": k,
        "rerank": rerank,
        "candidates": candidates,
        "adapter": {"name": adapter.name, "version": adapter.version},
        "judge_model": judge.model if judge is not None else None,
        # Eval retrieves from library items only (the RAG default), so repo docs never move a
        # baseline; recorded so a stored run says which corpora it covered.
        "corpora": ["item"],
        "server": (client.status() or {}).get("retrieval", {}),
    }
    _note_item_and_warmup(config, eval_set, current_sha, warmup)

    outcomes, errors = _retrieve_all(
        client,
        samples,
        k=k,
        mode=mode,
        rerank=rerank,
        candidates=candidates,
        warmup=warmup,
        on_retrieval=on_retrieval,
    )
    ok = [i for i, o in enumerate(outcomes) if o is not None]
    retrieved: list[list[Passage]] = [o.passages if o else [] for o in outcomes]
    failed = [{"sample_id": samples[i].id, "error": errors[i]} for i, e in enumerate(errors) if e]
    if failed:
        config["error"] = failed[0]["error"]
        config["errors"] = failed

    run = _build_run(eval_set, samples, config, outcomes, errors)
    vis = _Visibility(client, {int(eval_set.item_id): item.get("visibility")})
    whole = SuiteContext(
        samples=samples,
        retrieved=retrieved,
        k=k,
        item=item,
        judge=judge,
        llm=llm,
        is_public=vis.public,
        latencies_ms=[o.latency_ms if o else None for o in outcomes],
        errors=errors,
        current_chunks=freshness_suite.chunks_lookup(client),
    )
    succeeded = dataclasses.replace(
        whole,
        samples=[samples[i] for i in ok],
        retrieved=[retrieved[i] for i in ok],
        latencies_ms=[whole.latencies_ms[i] for i in ok],
        errors=[None for _ in ok],
    )
    aggregates = _score_all(chosen, whole, succeeded, ok, adapter, run)
    aggregates[PRECISION] = _mean([run.samples[i].scores.get(PRECISION) for i in ok])
    aggregates[RECALL] = _mean([run.samples[i].scores.get(RECALL) for i in ok])
    _set_status(run, aggregates, ok, len(samples))
    return run


def _note_item_and_warmup(
    config: dict[str, Any], eval_set: EvalSet, current_sha: str | None, warmup: bool
) -> None:
    """Record a changed item (freshness reports its questions) and an untimed warm-up."""
    if current_sha != eval_set.item_sha256:
        config["item_changed"] = {"set_sha256": eval_set.item_sha256, "item_sha256": current_sha}
    if warmup:
        config["warmup"] = True


def _score_all(
    chosen: Sequence,
    whole: SuiteContext,
    succeeded: SuiteContext,
    ok: list[int],
    adapter: EvalAdapter,
    run: SuiteRun,
) -> dict[str, float | None]:
    """Score every chosen suite into ``run``; return the merged aggregates.

    Speed and freshness see every question; the other suites see only the questions whose
    retrieval succeeded. A skipped suite's reason goes to ``config.suites_skipped``.
    """
    aggregates: dict[str, float | None] = {}
    skipped: dict[str, str] = {}
    for suite in chosen:
        in_all = suite.name in ALL_QUESTION_SUITES
        rows = list(range(len(whole.samples))) if in_all else ok
        score = _score_suite(suite, whole if in_all else succeeded, adapter, run)
        if score is None:
            continue
        if score.skipped:
            skipped[suite.name] = score.skipped
            if suite.name == JUDGED:
                run.config["llm_metrics_skipped"] = score.skipped
            continue
        _merge_score(run, suite, score, rows, aggregates)
    if skipped:
        run.config["suites_skipped"] = skipped
    return aggregates


def _set_status(run: SuiteRun, aggregates: dict, ok: list[int], total: int) -> None:
    """complete (aggregates), partial (partial aggregates over ``ok``) or failed (none)."""
    if not total or len(ok) == total:
        run.status, run.aggregates = "complete", aggregates
    elif ok:
        run.status, run.aggregates, run.partial_aggregates = "partial", None, aggregates
    else:
        run.status, run.aggregates = "failed", None


def _score_suite(suite, ctx: SuiteContext, adapter: EvalAdapter, run: SuiteRun):
    """Score one suite; a judged suite runs the caller's adapter. None when scoring failed."""
    capture = None
    if suite.name == JUDGED and isinstance(suite, JudgedSuite):
        capture = _CapturingAdapter(adapter)
        suite = JudgedSuite(adapter_factory=lambda _judge: capture)
    try:
        score: SuiteScore = suite.score(ctx)
    except _RETRIEVAL_ERRORS as exc:
        if suite.name == JUDGED:
            # Retrieval and ID scoring are unaffected, so the run keeps its status; only the
            # LLM-judged metrics are missing, and the reason is recorded.
            run.config["llm_metrics_failed"] = str(exc)
        else:
            run.config.setdefault("suites_failed", {})[suite.name] = str(exc)
        return None
    if capture is not None and capture.result is not None and not score.skipped:
        score = _with_adapter_extras(score, capture.result)
    return score


def _with_adapter_extras(score: SuiteScore, raw: ScoreResult) -> SuiteScore:
    """Add the adapter's extra metrics (keys no other suite owns) under the judged ones."""
    owned = {m.name for s in registry.all_suites() if s.name != JUDGED for m in s.metrics}

    def merge(extra: dict, own: dict) -> dict:
        return {**{k: v for k, v in extra.items() if k not in owned}, **own}

    per = [merge(extra, own) for extra, own in zip(raw.per_sample, score.per_sample)]
    return SuiteScore(per, merge(raw.aggregate, score.aggregate))


__all__ = [
    "ALL_QUESTION_SUITES",
    "EvalStaleError",
    "SuiteRun",
    "SuiteSampleResult",
    "default_suites",
    "id_metrics",
    "precheck",
    "run_set",
]
