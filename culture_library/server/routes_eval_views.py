"""Eval views: radar, compare and weakest-sources over stored runs (``read`` scope).

All numbers come from :mod:`culture_library.core.eval_radar` over :mod:`eval_store` rows. Run
visibility follows the owning item (see ``routes_eval``): a single run the key cannot see (or
that does not exist) is a flat 404 ``not_found``; inside a group it is *left out* of every
mean, and the response only carries ``left_out`` (a count, never an id or a value from it).

Common query params: ``budget_s`` (float > 0, default 1.0), the latency budget of the
``latency_in_budget`` axis. Run lists are comma-separated ids (at most 200).

``GET /api/eval/radar``
    ``?run=22`` one run, ``?runs=11,12,13`` a group (mean over documents), either optionally
    with ``vs=<id|id,id,...>`` for a second run/group (pair for the overlay). Response:
    ``{kind: "run"|"group", axes, documents:[{run_id,set_id,questions,axes}], left_out,
    budget_s}``; a pair is ``{kind: "pair", a: <shape>, b: <shape>, budget_s}``.

``GET /api/eval/compare``
    ``?before=19&after=11`` or groups ``?before=1,2&after=3,4``. Response:
    ``{checks:{comparable,reasons,warnings}, before, after, deltas, changed_questions,
    left_out:{before,after}, budget_s}``; when not comparable only ``checks`` (and
    ``left_out``, ``budget_s``) are present. Groups pair runs by set id; an unpaired set is a
    warning.

``GET /api/eval/weakest``
    ``?runs=1,2,3&limit=10`` (``run=`` also works). Response: ``{sources:[{run_id,set_id,url,
    questions,axes}], items:[{run_id,set_id,item_id,questions,axes}], left_out, limit,
    budget_s}``, each list weakest first (found@5, then MRR), cut to ``limit`` (default 10).

New module for plan task t9.
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter, Depends

import culture_library.eval.suites  # noqa: F401  # installs the registry-driven radar axes
from culture_library.core import NotFoundError, Store, eval_radar, eval_store
from culture_library.server.auth import Principal, check, current_principal, get_store
from culture_library.server.routes_eval import _flat, _visible_set

router = APIRouter(tags=["eval"])

_MAX_RUNS = 200
_Scored = tuple[dict, list[dict], list[dict]]  # run (with item_id), results, set samples


class _Bad(Exception):
    def __init__(self, detail: str):
        self.detail = detail


def _ids(raw: str | None, name: str) -> list[int]:
    try:
        out = [int(p) for p in (raw or "").split(",") if p.strip()]
    except ValueError:
        raise _Bad(f"{name} must be comma-separated integer ids") from None
    if not out:
        raise _Bad(f"{name} is required")
    if len(out) > _MAX_RUNS:
        raise _Bad(f"{name} takes at most {_MAX_RUNS} ids")
    return list(dict.fromkeys(out))


def _budget(budget_s: float) -> float:
    if math.isnan(budget_s) or budget_s <= 0:
        raise _Bad("budget_s must be greater than 0")
    return budget_s


def _load(store: Store, principal: Principal, run_id: int) -> _Scored | None:
    """The run's scored rows, or None when absent or its item is invisible."""
    try:
        with store.reader() as conn:
            run = eval_store.get_run(conn, run_id)
    except NotFoundError:
        return None
    found = _visible_set(store, principal, run["set_id"])
    if found is None:
        return None
    run["item_id"] = found[0]["item_id"]
    with store.reader() as conn:
        results = eval_store.list_run_samples(conn, run_id)
        set_samples = eval_store.list_samples(conn, run["set_id"])
    return run, results, set_samples


def _load_group(store: Store, principal: Principal, ids: list[int]) -> tuple[list[_Scored], int]:
    loaded = [_load(store, principal, i) for i in ids]
    return [x for x in loaded if x is not None], sum(1 for x in loaded if x is None)


def _shape(scored: list[_Scored], left_out: int, kind: str, budget_s: float) -> dict[str, Any]:
    g = eval_radar.group([(r, s) for r, s, _ in scored], budget_s=budget_s)
    return {"kind": kind, **g, "left_out": left_out}


def _single_or_404(store: Store, principal: Principal, run_id: int):
    loaded = _load(store, principal, run_id)
    if loaded is None:
        return None, _flat(404, f"eval run {run_id} not found", "not_found")
    return loaded, None


def _side(
    store: Store,
    principal: Principal,
    run: str | None,
    runs: str | None,
    name: str,
    budget_s: float,
):
    """(shape, error response) for one radar side from ``run`` / ``runs`` params."""
    if (run is None) == (runs is None):
        raise _Bad(f"give exactly one of {name}run and {name}runs")
    if run is not None:
        rid = _ids(run, "run")
        if len(rid) != 1:
            raise _Bad("run takes a single id; use runs for a group")
        loaded, err = _single_or_404(store, principal, rid[0])
        if err:
            return None, err
        return _shape([loaded], 0, "run", budget_s), None
    scored, left = _load_group(store, principal, _ids(runs, "runs"))
    return _shape(scored, left, "group", budget_s), None


@router.get("/api/eval/radar")
def get_radar(
    run: str | None = None,
    runs: str | None = None,
    vs: str | None = None,
    budget_s: float = 1.0,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
):
    check(principal, "read")
    try:
        budget = _budget(budget_s)
        a, err = _side(store, principal, run, runs, "", budget)
        if err:
            return err
        if vs is None:
            return {**a, "budget_s": budget}
        ids = _ids(vs, "vs")
        if len(ids) == 1:
            loaded, err = _single_or_404(store, principal, ids[0])
            if err:
                return err
            b = _shape([loaded], 0, "run", budget)
        else:
            scored, left = _load_group(store, principal, ids)
            b = _shape(scored, left, "group", budget)
        return {"kind": "pair", "a": a, "b": b, "budget_s": budget}
    except _Bad as exc:
        return _flat(422, exc.detail, "invalid")


def _group_compare(before: list[_Scored], after: list[_Scored], budget_s: float) -> dict:
    by_set = {r["set_id"]: (r, s) for r, s, _ in after}
    checks = {"comparable": True, "reasons": [], "warnings": []}
    changed: list[dict] = []
    matched = 0
    for r, s, _ in before:
        other = by_set.pop(r["set_id"], None)
        if other is None:
            checks["warnings"].append(f"set {r['set_id']} has no run on the after side")
            continue
        matched += 1
        part = eval_radar.compare((r, s), other, budget_s=budget_s)
        c = part["checks"]
        checks["reasons"] += [f"set {r['set_id']}: {x}" for x in c["reasons"]]
        checks["warnings"] += [f"set {r['set_id']}: {x}" for x in c["warnings"]]
        changed += [{**q, "set_id": r["set_id"]} for q in part.get("changed_questions", [])]
    checks["warnings"] += [f"set {sid} has no run on the before side" for sid in by_set]
    if not matched:
        checks["reasons"].append("no set has a run on both sides")
    checks["comparable"] = not checks["reasons"]
    if not checks["comparable"]:
        return {"checks": checks}
    ax_a = eval_radar.group([(r, s) for r, s, _ in before], budget_s=budget_s)["axes"]
    ax_b = eval_radar.group([(r, s) for r, s, _ in after], budget_s=budget_s)["axes"]
    deltas = {
        a: None if ax_a[a] is None or ax_b[a] is None else ax_b[a] - ax_a[a]
        for a in eval_radar.axis_names()
    }
    changed.sort(key=lambda c: c["delta"])
    return {
        "checks": checks,
        "before": ax_a,
        "after": ax_b,
        "deltas": deltas,
        "changed_questions": changed,
    }


@router.get("/api/eval/compare")
def get_compare(
    before: str,
    after: str,
    budget_s: float = 1.0,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
):
    check(principal, "read")
    try:
        budget = _budget(budget_s)
        ids_b, ids_a = _ids(before, "before"), _ids(after, "after")
    except _Bad as exc:
        return _flat(422, exc.detail, "invalid")
    left = {"before": 0, "after": 0}
    if len(ids_b) == 1 and len(ids_a) == 1:
        one_b, err = _single_or_404(store, principal, ids_b[0])
        if err:
            return err
        one_a, err = _single_or_404(store, principal, ids_a[0])
        if err:
            return err
        out = eval_radar.compare(one_b[:2], one_a[:2], budget_s=budget)
    else:
        grp_b, left["before"] = _load_group(store, principal, ids_b)
        grp_a, left["after"] = _load_group(store, principal, ids_a)
        if not grp_b or not grp_a:
            side = "before" if not grp_b else "after"
            checks = {
                "comparable": False,
                "reasons": [f"no visible runs on the {side} side"],
                "warnings": [],
            }
            out = {"checks": checks}
        else:
            out = _group_compare(grp_b, grp_a, budget)
    return {**out, "left_out": left, "budget_s": budget}


def _weak_key(row: dict) -> tuple[float, float]:
    ax = row["axes"]
    return (ax["found_at_5"] if ax["found_at_5"] is not None else 1.0, ax["mrr"] or 0.0)


@router.get("/api/eval/weakest")
def get_weakest(
    run: str | None = None,
    runs: str | None = None,
    limit: int = 10,
    budget_s: float = 1.0,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
):
    check(principal, "read")
    try:
        budget = _budget(budget_s)
        if (run is None) == (runs is None):
            raise _Bad("give exactly one of run and runs")
        ids = _ids(run if run is not None else runs, "run" if run is not None else "runs")
        if limit < 1:
            raise _Bad("limit must be at least 1")
    except _Bad as exc:
        return _flat(422, exc.detail, "invalid")
    scored, left = _load_group(store, principal, ids)
    if run is not None and not scored:
        return _flat(404, f"eval run {ids[0]} not found", "not_found")
    sources: list[dict] = []
    items: list[dict] = []
    for r, results, set_samples in scored:
        bd = eval_radar.breakdown(r, results, set_samples, budget_s=budget)
        tag = {"run_id": r["id"], "set_id": r["set_id"]}
        sources += [{**tag, **row} for row in bd["sources"]]
        items += [{**tag, **row} for row in bd["items"]]
    sources.sort(key=_weak_key)
    items.sort(key=_weak_key)
    return {
        "sources": sources[:limit],
        "items": items[:limit],
        "left_out": left,
        "limit": limit,
        "budget_s": budget,
    }
