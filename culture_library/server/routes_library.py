"""``/api/library``: upload, list/search, get, download, archive and restore catalog items.

Provenance: cited (cite-don't-import) from agentculture/xteink @ ea2f643 (branch
feat/issue-1), path xteink/server/routes_library.py; this copy is owned here. The wire
contract xteink clients rely on (culture-rules' ``library.push``) is unchanged:

- ``GET /api/library?q=&kind=&limit=&offset=`` -> ``{"items": [Item, ...]}``
- ``POST /api/library`` multipart ``file`` (+ ``title``, ``author``, ``kind``) -> 201
  ``{"item": Item, "created": true}``, or 200 with ``created: false`` for known bytes
- ``GET /api/library/{id}`` -> Item; ``GET /api/library/{id}/file`` -> the stored bytes
- ``DELETE /api/library/{id}`` -> 204

Modified: any of the kinds in :data:`culture_library.core.KINDS` (xteink's ``book`` and
``article`` included); a ``visibility`` form field (``private`` when omitted, decision c43);
scoped keys (read for reads, write for changes, per-key kind lists); private items are 404
to callers without ``private``, except to the key that uploaded them (d4), which can read,
archive and restore its own items; ``GET /api/library/{id}`` adds ``uploaded_by_you``;
DELETE *archives* (decision c45) and
``POST /api/library/{id}/restore`` undoes it within 30 days; ``?include_archived=1`` on get
and file shows archived items to private-scope callers. Every error body on these routes,
auth and request-shape errors included, is a flat ``{"detail": str, "code": str}``: ingest
refusals are 413 (``too_large``), 415 (``unsupported_format``) or 400 (any other code).
The upload body is capped while it streams (60 MiB + 1 MiB of form framing; a larger
``Content-Length`` is refused before reading). Bytes matching a private item answer 409
``conflict`` without an id to a caller lacking private access, unless that caller's key
uploaded it (its idempotent retry). Bytes matching an *archived* item the caller can see but
whose kind its key may not write answer 403 ``forbidden_kind`` and the item stays archived
(the restore route's rule); a live one is returned as-is. A successful upload wakes the
search indexer, if running.

The router's lifespan starts a :class:`~culture_library.core.library.Purger` that runs the
30-day purge every ``CULTURE_LIBRARY_PURGE_INTERVAL_S`` seconds (default 3600; ``0`` turns it
off, e.g. when cron runs ``purge_expired`` instead).
"""

from __future__ import annotations

import importlib
import re
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any, AsyncIterator, Callable

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute

from culture_library.core import NotFoundError, Store, ValidationError, library
from culture_library.server.auth import Principal, check, current_principal, get_store

UPLOAD_LIMITS = library.IngestLimits()

_STATUS_BY_CODE = {"too_large": 413, "unsupported_format": 415}
_MEDIA_TYPES = {
    "epub": "application/epub+zip",
    "pdf": "application/pdf",
    "md": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "json": "application/json",
}
_UNSAFE_FILENAME = re.compile(r"[^\w .()\-]+")
_CODE_BY_STATUS = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "not_allowed"}


def _flat(status: int, detail: str, code: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"detail": detail, "code": code}, status_code=status, headers=headers)


def _validation_detail(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path"))
        parts.append(f"{loc}: {err.get('msg', 'invalid')}" if loc else str(err.get("msg")))
    return "; ".join(parts) or "invalid request"


class _BodyTooLarge(HTTPException):
    """Raised from ``receive`` once a capped body grows past its cap (FastAPI re-raises
    an HTTPException from body parsing unchanged)."""

    def __init__(self) -> None:
        super().__init__(413, {"detail": "file exceeds the size limit", "code": "too_large"})


def _capped(request: Request, cap: int) -> Request:
    """``request`` whose body stream raises :class:`_BodyTooLarge` past ``cap`` bytes.

    A declared ``Content-Length`` over the cap is refused before anything is read; a chunked
    body is refused as soon as the running total crosses the cap, while the form parser is
    still streaming it (file parts spool to disk, they are never all held in memory).
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        raise _BodyTooLarge()
    receive = request.receive
    seen = 0

    async def capped_receive():
        nonlocal seen
        message = await receive()
        if message.get("type") == "http.request":
            seen += len(message.get("body", b""))
            if seen > cap:
                raise _BodyTooLarge()
        return message

    return Request(request.scope, capped_receive)


def max_body(cap: Callable[[], int]) -> Callable:
    """Mark an endpoint so :class:`FlatErrorRoute` caps its request body at ``cap()``."""

    def mark(endpoint: Callable) -> Callable:
        endpoint.max_body_bytes = cap  # type: ignore[attr-defined]
        return endpoint

    return mark


class FlatErrorRoute(APIRoute):
    """Render every error raised while handling a library route as ``{detail, code}``.

    The shared auth helpers raise ``HTTPException(detail={detail, code})``, which FastAPI
    would nest under ``detail``; xteink clients read a top-level ``code``. Endpoints marked
    with :func:`max_body` get their body capped while it is read.
    """

    def get_route_handler(self) -> Callable:
        handler = super().get_route_handler()
        cap = getattr(self.endpoint, "max_body_bytes", None)

        async def route(request: Request) -> Response:
            try:
                if cap is not None:
                    request = _capped(request, cap())
                return await handler(request)
            except HTTPException as exc:
                detail: Any = exc.detail
                if isinstance(detail, dict) and "code" in detail:
                    text, code = str(detail.get("detail", "")), str(detail["code"])
                else:
                    text = str(detail)
                    code = _CODE_BY_STATUS.get(exc.status_code, "error")
                return _flat(exc.status_code, text, code, exc.headers)
            except RequestValidationError as exc:
                return _flat(422, _validation_detail(exc), "invalid_request")

        return route


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    interval = library.purge_interval_from_env()
    purger = None
    if interval > 0:
        store = getattr(app.state, "store", None)
        if store is None:
            store = app.state.store = Store()
        purger = app.state.library_purger = library.Purger(store, interval)
        purger.start()
    try:
        yield
    finally:
        if purger is not None:
            purger.stop()


router = APIRouter(
    prefix="/api/library", tags=["library"], route_class=FlatErrorRoute, lifespan=_lifespan
)


def _refused(exc: ValidationError) -> JSONResponse:
    return _flat(_STATUS_BY_CODE.get(exc.code, 400), exc.detail, exc.code)


def _not_found(item_id: int) -> JSONResponse:
    return _flat(404, f"item {item_id} not found", "not_found")


def _item(item) -> dict:
    return asdict(item)


def _vis(principal: Principal) -> dict:
    """Visibility arguments for the core: private access, or the caller's own uploads."""
    return {"include_private": principal.private, "owner": principal.key_id}


def _can_see(principal: Principal, store: Store, item_id: int) -> bool:
    """Whether this caller may learn *item_id* exists (the GET rule; private sees archived)."""
    try:
        library.get(store, item_id, include_archived=principal.private, **_vis(principal))
    except NotFoundError:
        return False
    return True


@router.get("")
def list_items(
    q: str | None = Query(None, description="search title/author"),
    kind: str | None = None,
    limit: int = Query(library.DEFAULT_LIMIT, ge=1, le=library.MAX_LIMIT),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
):
    """List live items (optionally by kind) or search title/author with ``q``."""
    check(principal, "read")
    try:
        items = library.list_items(
            store, q=q, kind=kind, limit=limit, offset=offset, **_vis(principal)
        )
    except ValidationError as exc:
        return _refused(exc)
    return {"items": [_item(i) for i in items]}


_FORM_SLACK = 1024 * 1024  # room for the multipart framing and the text fields


def _upload_cap() -> int:
    return UPLOAD_LIMITS.max_bytes + _FORM_SLACK


def _notify_indexer(store: Store) -> None:
    """Wake the background indexer for ``store`` if one is running (never starts one).

    The indexer is owned by ``routes_search``: its ``notify_indexer(store)`` when it has one,
    else the matching entry of its running indexers. Absent module: nothing to wake.
    """
    try:
        owner = importlib.import_module("culture_library.server.routes_search")
    except ImportError:
        return
    notify = getattr(owner, "notify_indexer", None)
    if callable(notify):
        notify(store)
        return
    for running in getattr(owner, "_indexers", ()):
        if getattr(running, "store", None) is store:
            running.notify()


@router.post("", status_code=201, responses={200: {"description": "already in the library"}})
@max_body(_upload_cap)
async def upload_item(
    response: Response,
    file: UploadFile = File(...),
    title: str | None = Form(None),
    author: str = Form(""),
    kind: str | None = Form(None),
    visibility: str | None = Form(None),
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
):
    """Upload one file (EPUB, PDF, Markdown, text or JSON), stored as-is.

    ``title`` defaults to the filename stem, ``kind`` to ``book`` for EPUB else
    ``document``, ``visibility`` to ``private``. Bytes matching a private item answer 409
    ``conflict`` (no id) to callers without private access, except the key that uploaded it.
    """
    check(principal, "write")
    filename = file.filename or ""
    kind = kind or None
    visibility = visibility or None
    try:
        kind = library.check_kind(kind or library.default_kind(filename))
        library.check_visibility(visibility)
    except ValidationError as exc:
        return _refused(exc)
    check(principal, "write", kind)
    # Read at most one byte past the cap so an oversize file is refused as too_large.
    data = await file.read(UPLOAD_LIMITS.max_bytes + 1)
    try:
        item, created = await run_in_threadpool(
            library.add,
            store,
            data,
            filename,
            kind=kind,
            title=title,
            author=author,
            visibility=visibility,
            include_private=principal.private,
            uploader=principal.key_id,
            writable_kinds=principal.kinds,
            limits=UPLOAD_LIMITS,
        )
    except library.ConflictError as exc:
        return _flat(409, str(exc), exc.code)
    except library.ForbiddenKindError as exc:
        return _flat(403, str(exc), exc.code)
    except ValidationError as exc:
        return _refused(exc)
    _notify_indexer(store)
    if not created:
        response.status_code = 200
    return {"item": _item(item), "created": created}


@router.get("/{item_id}")
def get_item(
    item_id: int,
    include_archived: bool = False,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
):
    check(principal, "read")
    try:
        item = library.get(
            store,
            item_id,
            **_vis(principal),
            include_archived=include_archived and principal.private,
        )
    except NotFoundError:
        return _not_found(item_id)
    return {**_item(item), "uploaded_by_you": library.uploaded_by(store, item.id, principal.key_id)}


@router.get("/{item_id}/file", response_class=FileResponse)
def download_item(
    item_id: int,
    include_archived: bool = False,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
):
    check(principal, "read")
    try:
        item = library.get(
            store,
            item_id,
            **_vis(principal),
            include_archived=include_archived and principal.private,
        )
    except NotFoundError:
        return _not_found(item_id)
    path = store.blob_path(item.sha256)
    if not path.is_file():
        return _flat(404, f"item {item_id} has no stored file", "file_missing")
    stem = _UNSAFE_FILENAME.sub("_", item.title).strip(" ._") or f"item-{item.id}"
    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(item.format, "application/octet-stream"),
        filename=f"{stem[:120]}.{item.format}",
    )


def _writable(principal: Principal, store: Store, item_id: int):
    check(principal, "write")
    item = library.get(store, item_id, **_vis(principal), include_archived=True)
    check(principal, "write", item.kind)
    return item


@router.delete("/{item_id}", status_code=204)
def delete_item(
    item_id: int,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
):
    """Archive the item (decision c45): hidden now, restorable for 30 days, then purged."""
    try:
        _writable(principal, store, item_id)
        library.archive(store, item_id, **_vis(principal))
    except NotFoundError:
        return _not_found(item_id)
    return Response(status_code=204)


@router.post("/{item_id}/restore")
def restore_item(
    item_id: int,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
):
    """Un-archive an item archived less than 30 days ago."""
    try:
        _writable(principal, store, item_id)
        item = library.restore(store, item_id, **_vis(principal))
    except NotFoundError:
        return _flat(404, f"archived item {item_id} not found", "not_found")
    return _item(item)


@router.put("/{item_id}/file")
@max_body(_upload_cap)
async def replace_item(
    item_id: int,
    file: UploadFile = File(...),
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
):
    """Swap the item's bytes under the same id; answer the updated item.

    Guarded like archive/restore (write scope and the item's kind) before the upload is read
    or validated; FastAPI has already spooled the multipart body, which ``max_body`` caps. A
    private, archived or absent item is 404. Refusals map like the upload. Bytes already held
    by another item are 409 ``duplicate_content``; ``other_id`` names that item only when the
    caller could GET it (private scope sees archived items too).
    """
    try:
        await run_in_threadpool(_writable, principal, store, item_id)
    except NotFoundError:
        return _not_found(item_id)
    # Read at most one byte past the cap so an oversize file is refused as too_large.
    data = await file.read(UPLOAD_LIMITS.max_bytes + 1)
    try:
        item = await run_in_threadpool(
            library.replace,
            store,
            item_id,
            data,
            file.filename or "",
            **_vis(principal),
            limits=UPLOAD_LIMITS,
        )
    except NotFoundError:
        return _not_found(item_id)
    except library.DuplicateContentError as exc:
        body = {"detail": "these bytes already belong to another item", "code": exc.code}
        if await run_in_threadpool(_can_see, principal, store, exc.other_id):
            body["other_id"] = exc.other_id
        return JSONResponse(body, status_code=409)
    except ValidationError as exc:
        return _refused(exc)
    _notify_indexer(store)
    return _item(item)
