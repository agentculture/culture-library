"""Server-side, judge-free eval runs: start one, poll it, close it out on restart.

``POST /api/eval/runs/start`` with ``{set_id, suites?, mode? (semantic|hybrid), k? (1-50),
rerank?, candidates? (k..200), warmup?}`` needs the ``write`` scope for the set's item kind
(as ``POST /api/eval/runs`` does). It checks the set, the suites and the item (archived,
missing or not fully indexed is refused at once, no run recorded), stores a run marked
running, starts :func:`culture_library.eval.runner.run_set` in a background thread and answers
202 ``{run_id, set_id, status: "running", total, suites, config}`` at once. Poll it with
``GET /api/eval/runs/{id}`` (``status: "running"`` with ``completed``/``total`` until done).

* Suites default to every judge-free suite. A suite that ``needs_judge`` is refused with 422
  ``needs_judge`` and a ``hint``: judged runs go through the CLI (``culture-library eval
  run``) or the Python client, because the server never imports ragas or calls a judge.
* A second start for a set that already has a running run is 409 ``{detail, code: "running",
  run_id}`` naming that run.
* The run retrieves *in process* with the starting key's visibility: an
  :class:`InProcessClient` calls the same handlers as ``POST /api/rag/retrieve`` and ``GET
  /api/rag/items/{id}/chunks`` with that key's :class:`Principal`, so a run never retrieves an
  item the key cannot see. Latency is therefore measured without HTTP; the run records
  ``config.latency_source = "in-process"``.
* Each finished question's retrieval is stored as it completes (and ``completed`` advanced),
  then replaced by the scored result when the run ends. A worker crash closes the run
  ``failed`` with ``config.error``.
* On app startup (this router's lifespan) every run still marked running was interrupted by
  the restart: it is closed ``partial`` over the questions it stored, or ``failed`` if none,
  with ``config.closed_reason``.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Annotated, Any, AsyncIterator, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, model_validator

from culture_library.core import NotFoundError, Store, eval_store, library
from culture_library.eval import suites as registry
from culture_library.eval.errors import EvalApiError, EvalError
from culture_library.eval.models import EvalSet, Passage
from culture_library.eval.runner import (
    EvalStaleError,
    IdOnlyAdapter,
    default_suites,
    id_metrics,
    precheck,
    run_set,
)
from culture_library.server import routes_rag, routes_status
from culture_library.server.auth import Principal, check, current_principal, get_store
from culture_library.server.routes_eval import _flat, _visible_set

log = logging.getLogger("culture_library.server.eval_jobs")

LATENCY_SOURCE = "in-process"
CLOSED_REASON = "server restarted while running"
JUDGED_HINT = (
    "judged suites need an LLM judge, which the server does not run; use "
    "`culture-library eval run <set_id>` or the Python client (eval.runner.run_set)"
)
_CHUNKS_PATH = re.compile(r"^/api/rag/items/(\d+)/chunks$")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Close out runs a previous process left marked running."""
    store = getattr(app.state, "store", None)
    if store is None:
        store = app.state.store = Store()
    close_out(store)
    yield


router = APIRouter(tags=["eval"], lifespan=_lifespan)


def close_out(store: Store) -> list[int]:
    """Close every run still marked running; return their ids."""
    with store.connect() as conn:
        closed = eval_store.close_running_runs(conn, CLOSED_REASON)
    if closed:
        log.warning("closed %d eval run(s) interrupted by a restart: %s", len(closed), closed)
    return closed


class StartIn(BaseModel):
    set_id: int
    suites: list[str] | None = None
    mode: Literal["semantic", "hybrid"] = "hybrid"
    k: int = Field(5, ge=1, le=50)
    rerank: bool | None = None
    candidates: int | None = Field(None, ge=1, le=200)
    warmup: bool = False

    @model_validator(mode="after")
    def _candidates_cover_k(self) -> StartIn:
        if self.candidates is not None and self.candidates < self.k:
            raise ValueError("candidates must be at least k")
        return self


def _fake_request(app: FastAPI) -> Request:
    """A minimal request carrying ``app`` (the handlers read only ``request.app``)."""
    return Request({"type": "http", "app": app, "method": "GET", "path": "/", "headers": []})


class InProcessClient:
    """The :class:`~culture_library.eval.client.LibraryClient` methods ``run_set`` uses,
    answered in process by the server's own handlers with one caller's visibility."""

    def __init__(self, app: FastAPI, store: Store, principal: Principal):
        self._app = app
        self._store = store
        self._principal = principal

    def _call(self, what: str, fn, *args):
        try:
            return fn(*args)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"detail": exc.detail}
            raise EvalApiError(
                f"{what} -> HTTP {exc.status_code}", status=exc.status_code, body=detail
            ) from None

    def status(self) -> dict:
        return routes_status.collect(self._store, self._principal.private, self._app.state)

    def get_item(self, item_id: int) -> dict:
        try:
            item = library.get(
                self._store,
                int(item_id),
                include_private=self._principal.private,
                owner=self._principal.key_id,
            )
        except NotFoundError:
            raise EvalApiError(f"item {item_id} not found", status=404) from None
        return asdict(item)

    def request(self, method: str, path: str) -> Any:
        match = _CHUNKS_PATH.match(path)
        if method != "GET" or match is None:
            raise EvalApiError(f"{method} {path} is not served in process", status=404)
        return self._call(
            f"{method} {path}",
            routes_rag.item_chunks,
            int(match.group(1)),
            _fake_request(self._app),
            self._principal,
        )

    def retrieve(self, query: str, k: int = 5, mode: str = "hybrid", **opts: Any) -> list[Passage]:
        params = {"query": query, "k": k, "mode": mode}
        params.update({k_: v for k_, v in opts.items() if v is not None})
        try:
            body = routes_rag.RetrieveRequest(**params)
        except ValidationError as exc:
            raise EvalApiError(f"invalid retrieve request: {exc}", status=422) from None
        data = self._call(
            "POST /api/rag/retrieve",
            routes_rag.retrieve,
            body,
            _fake_request(self._app),
            self._principal,
        )
        return [Passage.from_dict(p) for p in data["passages"]]


class _ProgressClient(InProcessClient):
    """Stores each question's retrieval as it finishes and advances ``completed``."""

    def __init__(self, app, store, principal, run_id: int, eval_set: EvalSet, warmup: bool):
        super().__init__(app, store, principal)
        self._run_id = run_id
        self._samples = list(eval_set.samples)
        self._version = eval_set.version
        self._skip = 1 if warmup and self._samples else 0  # run_set's un-timed warmup call
        self._done = 0

    def retrieve(self, query: str, k: int = 5, mode: str = "hybrid", **opts: Any) -> list[Passage]:
        if self._skip:
            self._skip -= 1
            return super().retrieve(query, k, mode, **opts)
        start = time.perf_counter()
        passages: list[Passage] | None = None
        try:
            passages = super().retrieve(query, k, mode, **opts)
            return passages
        finally:
            self._record(query, passages, (time.perf_counter() - start) * 1000.0)

    def _record(self, query: str, passages: list[Passage] | None, latency_ms: float) -> None:
        index = self._done
        self._done += 1
        with self._store.connect() as conn:
            sample = self._samples[index] if index < len(self._samples) else None
            if passages is not None and sample is not None and sample.question == query:
                shas = [p.chunk_sha for p in passages]
                eval_store.add_run_sample(
                    conn,
                    self._run_id,
                    sample.id,
                    retrieved_chunk_shas=shas,
                    scores=id_metrics(shas, sample.reference_chunk_shas),
                    question=sample.question,
                    reference_chunk_shas=list(sample.reference_chunk_shas),
                    set_version=self._version,
                    retrieved_item_ids=[p.item_id for p in passages],
                    latency_ms=round(latency_ms, 3),
                )
            eval_store.set_run_progress(conn, self._run_id, self._done)


def _load_set(store: Store, set_id: int) -> EvalSet:
    with store.reader() as conn:
        row = eval_store.get_set(conn, set_id)
        samples = eval_store.list_samples(conn, set_id)
    return EvalSet.from_dict(
        {
            "id": row["id"],
            "item_id": row["item_id"],
            "item_sha256": row["item_sha256"],
            "adapter": row["adapter"],
            "adapter_version": row["adapter_version"],
            "chunker": row["chunker_json"],
            "generator": row["generator_json"],
            "version": row["version"],
            "samples": samples,
        }
    )


def _choose_suites(names: list[str] | None) -> list[str] | JSONResponse:
    if names is None:
        return default_suites()
    chosen = list(dict.fromkeys(names))
    try:
        found = [registry.get(n) for n in chosen]
    except KeyError as exc:
        return _flat(422, exc.args[0], "invalid")
    judged = [s.name for s in found if s.needs_judge]
    if judged:
        return JSONResponse(
            {
                "detail": f"suite(s) {', '.join(judged)} need a judge and cannot run on the server",
                "code": "needs_judge",
                "hint": JUDGED_HINT,
            },
            status_code=422,
        )
    return chosen


def _worker(
    app: FastAPI,
    store: Store,
    principal: Principal,
    run_id: int,
    eval_set: EvalSet,
    body: StartIn,
    suites: list[str],
    base_config: dict,
) -> None:
    client = _ProgressClient(app, store, principal, run_id, eval_set, body.warmup)
    try:
        run = run_set(
            client,
            eval_set,
            IdOnlyAdapter(),
            mode=body.mode,
            k=body.k,
            rerank=body.rerank,
            candidates=body.candidates,
            suites=suites,
            warmup=body.warmup,
        )
    except Exception as exc:  # any failure closes the run; nothing escapes the thread
        log.exception("eval run %d failed", run_id)
        with store.connect() as conn:
            eval_store.finish_run(
                conn,
                run_id,
                status="failed",
                config={**base_config, "error": str(exc) or type(exc).__name__},
                completed=0,
            )
        return
    config = {**run.config, "latency_source": LATENCY_SOURCE}
    with store.connect() as conn:
        for s in run.samples:
            eval_store.add_run_sample(
                conn,
                run_id,
                s.sample_id,
                retrieved_chunk_shas=s.retrieved_chunk_shas,
                scores=s.scores,
                question=s.question,
                reference_chunk_shas=s.reference_chunk_shas,
                set_version=s.set_version,
                retrieved_item_ids=s.retrieved_item_ids,
                latency_ms=s.latency_ms,
                freshness=s.freshness,
            )
        eval_store.finish_run(
            conn,
            run_id,
            status=run.status,
            config=config,
            completed=run.completed,
            aggregates=run.aggregates if run.status == "complete" else None,
            partial_aggregates=run.partial_aggregates,
            error_rate=run.error_rate,
            suites=run.suites,
        )


@router.post("/api/eval/runs/start", status_code=202)
def start_run(
    body: StartIn,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    store: Annotated[Store, Depends(get_store)],
):
    check(principal, "write")
    found = _visible_set(store, principal, body.set_id)
    if found is None:
        return _flat(404, f"eval set {body.set_id} not found", "not_found")
    check(principal, "write", found[1].kind)
    suites = _choose_suites(body.suites)
    if isinstance(suites, JSONResponse):
        return suites
    eval_set = _load_set(store, body.set_id)
    app = request.app
    try:
        precheck(InProcessClient(app, store, principal), eval_set)
    except EvalStaleError as exc:
        return JSONResponse(
            {"detail": str(exc), "code": "stale", "hint": exc.hint}, status_code=409
        )
    except EvalError as exc:
        return _flat(409, str(exc), "stale")
    config = {
        "mode": body.mode,
        "k": body.k,
        "rerank": body.rerank,
        "candidates": body.candidates,
        "suites": suites,
        "latency_source": LATENCY_SOURCE,
    }
    if body.warmup:
        config["warmup"] = True
    with store.connect() as conn:  # IMMEDIATE: the check and the insert are one write
        running = eval_store.running_runs(conn, body.set_id)
        if running:
            return JSONResponse(
                {
                    "detail": f"eval set {body.set_id} already has a running run {running[0]}",
                    "code": "running",
                    "run_id": running[0],
                },
                status_code=409,
            )
        run_id = eval_store.create_run(
            conn,
            body.set_id,
            status="partial",  # placeholder; ``running`` decides what readers see
            config=config,
            total=len(eval_set.samples),
            completed=0,
            running=True,
        )
    thread = threading.Thread(
        target=_worker,
        args=(app, store, principal, run_id, eval_set, body, suites, config),
        name=f"eval-run-{run_id}",
        daemon=True,
    )
    jobs = getattr(app.state, "eval_jobs", None)
    if jobs is None:
        jobs = app.state.eval_jobs = {}
    jobs[run_id] = thread
    thread.start()
    return {
        "run_id": run_id,
        "set_id": body.set_id,
        "status": "running",
        "total": len(eval_set.samples),
        "suites": suites,
        "config": config,
    }


__all__ = ["CLOSED_REASON", "InProcessClient", "LATENCY_SOURCE", "close_out", "router"]
