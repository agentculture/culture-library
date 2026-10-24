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
to callers without ``private``; DELETE *archives* (decision c45) and
``POST /api/library/{id}/restore`` undoes it within 30 days; ``?include_archived=1`` on get
and file shows archived items to private-scope callers. Every error body on these routes,
auth and request-shape errors included, is a flat ``{"detail": str, "code": str}``: ingest
refusals are 413 (``too_large``), 415 (``unsupported_format``) or 400 (any other code).

The router's lifespan starts a :class:`~culture_library.core.library.Purger` that runs the
30-day purge every ``CULTURE_LIBRARY_PURGE_INTERVAL_S`` seconds (default 3600; ``0`` turns it
off, e.g. when cron runs ``purge_expired`` instead).
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from dataclasses import asdict
from email.parser import BytesParser
from email.policy import HTTP
from typing import Any, AsyncIterator, Callable

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute

from culture_library.core import KINDS, VISIBILITIES, NotFoundError, Store, ValidationError, library
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


class FlatErrorRoute(APIRoute):
    """Render every error raised while handling a library route as ``{detail, code}``.

    The shared auth helpers raise ``HTTPException(detail={detail, code})``, which FastAPI
    would nest under ``detail``; xteink clients read a top-level ``code``.
    """

    def get_route_handler(self) -> Callable:
        handler = super().get_route_handler()

        async def route(request: Request) -> Response:
            try:
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
            store, q=q, kind=kind, limit=limit, offset=offset, include_private=principal.private
        )
    except ValidationError as exc:
        return _refused(exc)
    return {"items": [_item(i) for i in items]}


_UPLOAD_SCHEMA = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {"type": "string", "format": "binary"},
                        "title": {"type": "string"},
                        "author": {"type": "string"},
                        "kind": {"type": "string", "enum": list(KINDS)},
                        "visibility": {"type": "string", "enum": list(VISIBILITIES)},
                    },
                }
            }
        },
    }
}
_FORM_SLACK = 1024 * 1024  # room for the multipart framing and the text fields


class _Form:
    def __init__(self) -> None:
        self.fields: dict[str, str] = {}
        self.filename: str | None = None
        self.data: bytes | None = None


def _parse_form(content_type: str, body: bytes) -> _Form:
    """Parse ``multipart/form-data`` with the stdlib ``email`` parser (binary-exact).

    Stdlib on purpose: python-multipart is only in the ``[server]`` extra, and the route
    module must import wherever ``create_app`` does. First occurrence of a name wins.
    """
    head = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1")
    msg = BytesParser(policy=HTTP).parsebytes(head + body)
    form = _Form()
    if not msg.is_multipart():
        return form
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str):
            continue
        payload = part.get_payload(decode=True) or b""
        if name == "file" and part.get_filename() is not None:
            if form.data is None:
                form.filename, form.data = part.get_filename() or "", payload
        elif name not in form.fields:
            form.fields[name] = payload.decode("utf-8", errors="replace")
    return form


async def _read_capped(request: Request, cap: int) -> bytes | None:
    """The request body, or ``None`` once it grows past ``cap`` bytes."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        return None
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > cap:
            return None
    return bytes(buf)


@router.post(
    "",
    status_code=201,
    responses={200: {"description": "already in the library"}},
    openapi_extra=_UPLOAD_SCHEMA,
)
async def upload_item(
    request: Request,
    response: Response,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
):
    """Upload one file (EPUB, PDF, Markdown, text or JSON), stored as-is.

    Multipart fields: ``file`` (required), ``title`` (default: the filename stem),
    ``author``, ``kind`` (default ``book`` for EPUB, else ``document``), ``visibility``
    (default ``private``).
    """
    check(principal, "write")
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        return _flat(422, "expected multipart/form-data with a file field", "invalid_request")
    body = await _read_capped(request, UPLOAD_LIMITS.max_bytes + _FORM_SLACK)
    if body is None:
        return _flat(413, "file exceeds the size limit", "too_large")
    form = await run_in_threadpool(_parse_form, content_type, body)
    if form.data is None:
        return _flat(422, "file: field required", "invalid_request")
    filename = form.filename or ""
    kind = form.fields.get("kind") or None
    visibility = form.fields.get("visibility") or None
    try:
        kind = library.check_kind(kind or library.default_kind(filename))
        library.check_visibility(visibility)
    except ValidationError as exc:
        return _refused(exc)
    check(principal, "write", kind)
    try:
        item, created = await run_in_threadpool(
            library.add,
            store,
            form.data,
            filename,
            kind=kind,
            title=form.fields.get("title"),
            author=form.fields.get("author", ""),
            visibility=visibility,
            limits=UPLOAD_LIMITS,
        )
    except ValidationError as exc:
        return _refused(exc)
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
            include_private=principal.private,
            include_archived=include_archived and principal.private,
        )
    except NotFoundError:
        return _not_found(item_id)
    return _item(item)


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
            include_private=principal.private,
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
    item = library.get(store, item_id, include_private=principal.private, include_archived=True)
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
        library.archive(store, item_id, include_private=principal.private)
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
        item = library.restore(store, item_id, include_private=principal.private)
    except NotFoundError:
        return _flat(404, f"archived item {item_id} not found", "not_found")
    return _item(item)
