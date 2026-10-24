"""Repo-document routes: push versioned specs/plans/devague records, read them back.

``POST /api/repodocs`` takes ``{remote, path, commit, committed_at, default_branch_head,
visibility, content}``; the rules (identity, current, visibility) live in
:mod:`culture_library.core.repodocs`. Private docs and versions are visible only to a
principal with ``private`` set; to anyone else they behave as absent (404 / omitted).

The store comes from ``app.state.store`` when set (tests), else a :class:`Store` on the
default data dir, created once per app. New module, written for plan task t8.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from culture_library.core import NotFoundError, Store, ValidationError
from culture_library.core.repodocs import RepoDocs, infer_kind, normalize_path
from culture_library.server.auth import Principal, check, current_principal

router = APIRouter(tags=["repodocs"])


class RepoDocPush(BaseModel):
    remote: str
    path: str
    commit: str
    committed_at: str
    default_branch_head: str
    visibility: str | None = None
    content: str


def get_repodocs(request: Request) -> RepoDocs:
    state = request.app.state
    store = getattr(state, "store", None)
    if store is None:
        store = state.store = Store()
    return RepoDocs(store)


def _bad(exc: ValidationError) -> HTTPException:
    return HTTPException(status_code=400, detail={"detail": str(exc), "code": "invalid"})


def _absent(what: str) -> HTTPException:
    return HTTPException(
        status_code=404, detail={"detail": f"{what} not found", "code": "not_found"}
    )


@router.post("/api/repodocs")
def push_repodoc(
    body: RepoDocPush,
    principal: Principal = Depends(current_principal),
    docs: RepoDocs = Depends(get_repodocs),
) -> JSONResponse:
    try:
        kind = infer_kind(normalize_path(body.path))
    except ValidationError as exc:
        raise _bad(exc) from exc
    check(principal, "write", kind)
    try:
        result = docs.push(**body.model_dump())
    except ValidationError as exc:
        raise _bad(exc) from exc
    payload = {
        "doc": asdict(result.doc),
        "version": asdict(result.version),
        "created": result.created,
        "current_changed": result.current_changed,
    }
    return JSONResponse(payload, status_code=201 if result.created else 200)


@router.get("/api/repodocs")
def list_repodocs(
    remote: str | None = None,
    path: str | None = None,
    principal: Principal = Depends(current_principal),
    docs: RepoDocs = Depends(get_repodocs),
) -> dict:
    check(principal, "read")
    try:
        found = docs.list(repo=remote, path=path, include_private=principal.private)
    except ValidationError as exc:
        raise _bad(exc) from exc
    return {"docs": [asdict(d) for d in found]}


@router.get("/api/repodocs/{doc_id}")
def get_repodoc(
    doc_id: int,
    principal: Principal = Depends(current_principal),
    docs: RepoDocs = Depends(get_repodocs),
) -> dict:
    check(principal, "read")
    doc = docs.get(doc_id, include_private=principal.private)
    if doc is None:
        raise _absent(f"repo doc {doc_id}")
    versions = docs.versions(doc_id, include_private=principal.private)
    return {"doc": asdict(doc), "versions": [asdict(v) for v in versions]}


@router.get("/api/repodocs/{doc_id}/content")
def get_repodoc_content(
    doc_id: int,
    commit: str | None = None,
    principal: Principal = Depends(current_principal),
    docs: RepoDocs = Depends(get_repodocs),
) -> Response:
    check(principal, "read")
    try:
        data = docs.content(doc_id, commit, include_private=principal.private)
    except ValidationError as exc:
        raise _bad(exc) from exc
    except NotFoundError as exc:
        raise _absent(f"repo doc {doc_id} version") from exc
    return Response(data, media_type="text/plain; charset=utf-8")
