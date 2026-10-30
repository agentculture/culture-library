"""Eval routes: store and read RAG eval sets and runs.

Bodies and responses use the JSON shapes of ``culture_library.eval.models``
(``EvalSet.to_dict`` / ``EvalRun.to_dict``). ``POST /api/eval/sets`` stores a set with its
samples (version 1) and returns it; ``POST /api/eval/runs`` stores a run with its per-sample
results at the set's current version and answers 422 when aggregates arrive with a status
other than ``complete``. Reads are filtered by the owning item's visibility: an item the
caller cannot see behaves as absent (404 on direct reads, omitted from lists), so sample text
never leaves for an invisible item. Writes need the ``write`` scope for the item's kind.

New module, written for plan task t11 over :mod:`culture_library.core.eval_store`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictFloat, StrictInt, StrictStr, field_validator

from culture_library.core import NotFoundError, Store, ValidationError, eval_store, library
from culture_library.server.auth import Principal, check, current_principal, get_store

router = APIRouter(tags=["eval"])


class SampleIn(BaseModel):
    question: str
    reference_answer: str = ""
    reference_chunk_shas: list[str] = Field(default_factory=list)
    reference_contexts: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    id: int | None = None


class SetIn(BaseModel):
    item_id: int
    item_sha256: str
    adapter: str
    adapter_version: str = ""
    chunker: dict[str, Any] = Field(default_factory=dict)
    generator: dict[str, Any] = Field(default_factory=dict)
    samples: list[SampleIn] = Field(default_factory=list)


class SampleResultIn(BaseModel):
    sample_id: int
    retrieved_chunk_shas: list[str] = Field(default_factory=list)
    scores: dict[str, float | None] = Field(default_factory=dict)
    # Optional per-question snapshot (a run's own record of what it asked and got).
    question: StrictStr | None = None
    reference_chunk_shas: list[StrictStr] | None = None
    set_version: StrictInt | None = None
    retrieved_item_ids: list[StrictInt] | None = None
    latency_ms: StrictInt | StrictFloat | None = None
    freshness: Any = None


class SuiteRefIn(BaseModel):
    name: StrictStr
    version: StrictInt | StrictStr | None = None


class RunIn(BaseModel):
    set_id: int
    status: str = "complete"
    config: dict[str, Any] = Field(default_factory=dict)
    total: int = 0
    completed: int = 0
    aggregates: dict[str, float | None] | None = None
    partial_aggregates: dict[str, float | None] | None = None
    error_rate: StrictInt | StrictFloat | None = Field(default=None, ge=0, le=1)
    suites: list[StrictStr | SuiteRefIn] | None = None
    samples: list[SampleResultIn] = Field(default_factory=list)

    @field_validator("suites")
    @classmethod
    def _normalise_suites(cls, v):
        if v is None:
            return None
        return [{"name": e, "version": None} if isinstance(e, str) else e.model_dump() for e in v]


def _flat(status: int, detail: str, code: str) -> JSONResponse:
    return JSONResponse({"detail": detail, "code": code}, status_code=status)


def _vis(principal: Principal) -> dict:
    return {"include_private": principal.private, "owner": principal.key_id}


def _item_or_none(store: Store, principal: Principal, item_id: int):
    """The item when this caller may see it (non-archived), else None."""
    try:
        return library.get(store, item_id, **_vis(principal))
    except NotFoundError:
        return None


def _set_json(row: dict) -> dict:
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "item_sha256": row["item_sha256"],
        "adapter": row["adapter"],
        "adapter_version": row["adapter_version"],
        "chunker": row["chunker_json"],
        "generator": row["generator_json"],
        "version": row["version"],
        "created_at": row["created_at"],
    }


def _sample_json(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "set_id"}


def _suite_entries(raw: Any) -> list[dict]:
    out = []
    for e in raw or []:
        out.append(dict(e) if isinstance(e, dict) else {"name": str(e), "version": None})
    return out


def _run_json(row: dict) -> dict:
    return {
        "id": row["id"],
        "set_id": row["set_id"],
        "set_version": row["set_version"],
        "status": "running" if row.get("running") else row["status"],
        "config": row["config_json"],
        "total": row["total"],
        "completed": row["completed"],
        "aggregates": row["aggregates_json"] or {},
        "partial_aggregates": row.get("partial_aggregates_json") or {},
        "error_rate": row.get("error_rate"),
        "suites": _suite_entries(row.get("suites_json")),
        "created_at": row["created_at"],
    }


def _run_sample_json(row: dict) -> dict:
    return {
        "sample_id": row["sample_id"],
        "retrieved_chunk_shas": row["retrieved_chunk_shas"],
        "scores": row["scores_json"],
        "question": row.get("question"),
        "reference_chunk_shas": row.get("reference_chunk_shas"),
        "set_version": row.get("set_version"),
        "retrieved_item_ids": row.get("retrieved_item_ids"),
        "latency_ms": row.get("latency_ms"),
        "freshness": row.get("freshness"),
    }


@router.post("/api/eval/sets", status_code=201)
def post_set(
    body: SetIn,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
):
    check(principal, "write")
    item = _item_or_none(store, principal, body.item_id)
    if item is None:
        return _flat(404, f"item {body.item_id} not found", "not_found")
    check(principal, "write", item.kind)
    if body.item_sha256 != item.sha256:
        return _flat(
            409,
            f"item {item.id} changed since the set was generated (now {item.sha256[:12]}); "
            "regenerate the set",
            "item_changed",
        )
    samples = [s.model_dump(exclude={"id"}) for s in body.samples]
    try:
        with store.connect() as conn:
            set_id = eval_store.create_set(
                conn,
                item.id,
                body.item_sha256,
                chunker=body.chunker,
                adapter=body.adapter,
                adapter_version=body.adapter_version,
                generator=body.generator,
            )
            eval_store.add_samples(conn, set_id, samples)
            row = eval_store.get_set(conn, set_id)
            stored = eval_store.list_samples(conn, set_id)
    except ValidationError as exc:
        return _flat(422, str(exc), "invalid")
    return {**_set_json(row), "samples": [_sample_json(s) for s in stored]}


@router.get("/api/eval/sets")
def list_sets(
    item_id: int | None = None,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
):
    check(principal, "read")
    with store.reader() as conn:
        rows = eval_store.list_sets(conn, item_id=item_id, **_vis(principal))
    return {"sets": [_set_json(r) for r in rows]}


def _visible_set(store: Store, principal: Principal, set_id: int):
    """(set row, item) when the set exists and its item is visible, else None."""
    try:
        with store.reader() as conn:
            row = eval_store.get_set(conn, set_id)
    except NotFoundError:
        return None
    item = _item_or_none(store, principal, row["item_id"])
    return None if item is None else (row, item)


@router.get("/api/eval/sets/{set_id}")
def get_set(
    set_id: int,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
):
    check(principal, "read")
    found = _visible_set(store, principal, set_id)
    if found is None:
        return _flat(404, f"eval set {set_id} not found", "not_found")
    with store.reader() as conn:
        samples = eval_store.list_samples(conn, set_id)
    return {**_set_json(found[0]), "samples": [_sample_json(s) for s in samples]}


@router.post("/api/eval/runs", status_code=201)
def post_run(
    body: RunIn,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
):
    check(principal, "write")
    found = _visible_set(store, principal, body.set_id)
    if found is None:
        return _flat(404, f"eval set {body.set_id} not found", "not_found")
    check(principal, "write", found[1].kind)
    if body.status not in eval_store.RUN_STATUSES:
        return _flat(
            422, f"run status must be one of {', '.join(eval_store.RUN_STATUSES)}", "invalid"
        )
    if body.status != "complete" and body.aggregates:
        return _flat(422, "aggregates are only accepted for a complete run", "invalid")
    aggregates = body.aggregates if body.status == "complete" else None
    try:
        with store.connect() as conn:
            known = {s["id"] for s in eval_store.list_samples(conn, body.set_id)}
            unknown = sorted({s.sample_id for s in body.samples} - known)
            if unknown:
                raise ValidationError(f"samples not in set {body.set_id}: {unknown}")
            run_id = eval_store.create_run(
                conn,
                body.set_id,
                status=body.status,
                config=body.config,
                total=body.total,
                completed=body.completed,
                aggregates=aggregates,
                partial_aggregates=body.partial_aggregates,
                error_rate=body.error_rate,
                suites=body.suites,
            )
            for s in body.samples:
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
            row = eval_store.get_run(conn, run_id)
            results = eval_store.list_run_samples(conn, run_id)
    except ValidationError as exc:
        return _flat(422, str(exc), "invalid")
    return {**_run_json(row), "samples": [_run_sample_json(r) for r in results]}


@router.get("/api/eval/runs")
def list_runs(
    set_id: int | None = None,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
):
    check(principal, "read")
    with store.reader() as conn:
        rows = eval_store.list_runs(conn, set_id=set_id, **_vis(principal))
    return {"runs": [_run_json(r) for r in rows]}


@router.get("/api/eval/runs/{run_id}")
def get_run(
    run_id: int,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
):
    check(principal, "read")
    try:
        with store.reader() as conn:
            row = eval_store.get_run(conn, run_id)
    except NotFoundError:
        row = None
    if row is None or _visible_set(store, principal, row["set_id"]) is None:
        return _flat(404, f"eval run {run_id} not found", "not_found")
    with store.reader() as conn:
        results = eval_store.list_run_samples(conn, run_id)
    return {**_run_json(row), "samples": [_run_sample_json(r) for r in results]}


@router.get("/api/eval/suites")
def get_suites(principal: Principal = Depends(current_principal)):
    """The suite registry with metric definitions (no ragas import)."""
    from culture_library.eval import suites

    check(principal, "read")
    return {"suites": suites.catalog()}
