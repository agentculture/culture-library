"""``/api/library/{id}/sources``: an item's sources sidecar (newspaper article provenance).

- ``PUT /api/library/{id}/sources`` (write scope; the key's kind list applies to the item's
  kind) with a bare JSON list ``[{article_title, source_urls[], excerpt}, ...]`` replaces the
  item's manifest and answers ``{"item_id", "sources", "updated_at"}``. Invalid bodies are
  422 ``invalid_request``.
- ``GET /api/library/{id}/sources`` (read scope) answers the same shape.

An item the caller cannot see is 404 ``not_found`` (the same answer as an absent one); a
visible item with no sidecar is 404 ``no_sources``. Sidecars are not catalog changes, so
writes append no feed event. Errors are the flat ``{detail, code}`` of the library routes.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends

from culture_library.core import NotFoundError, Store, library, sources
from culture_library.server.auth import Principal, check, current_principal, get_store
from culture_library.server.routes_library import FlatErrorRoute, _flat, _vis

MAX_ENTRIES = 500
MAX_EXCERPT = 2000

router = APIRouter(prefix="/api/library", tags=["sources"], route_class=FlatErrorRoute)


def _invalid(detail: str):
    return _flat(422, detail, "invalid_request")


def _bad_url(url: Any) -> bool:
    if not isinstance(url, str):
        return True
    parts = urlsplit(url)
    return parts.scheme not in ("http", "https") or not parts.netloc


def validate_manifest(body: Any) -> str | None:
    """A description of the first problem with ``body``, or ``None`` when it is valid."""
    if not isinstance(body, list):
        return "the body must be a JSON list of source entries"
    if len(body) > MAX_ENTRIES:
        return f"at most {MAX_ENTRIES} entries are allowed"
    for i, entry in enumerate(body):
        problem = _entry_problem(entry)
        if problem:
            return f"entry {i}{problem}"
    return None


def _entry_problem(entry: Any) -> str | None:
    """The problem with one manifest entry (as a suffix after "entry N"), or None."""
    if not isinstance(entry, dict):
        return " must be an object"
    title = entry.get("article_title")
    if not isinstance(title, str) or not title.strip():
        return ": article_title must be a non-empty string"
    urls = entry.get("source_urls")
    if not isinstance(urls, list) or not urls:
        return ": source_urls must be a non-empty list"
    if any(_bad_url(u) for u in urls):
        return ": source_urls must be http(s) URL strings"
    excerpt = entry.get("excerpt")
    if not isinstance(excerpt, str):
        return ": excerpt must be a string"
    if len(excerpt) > MAX_EXCERPT:
        return f": excerpt is longer than {MAX_EXCERPT} characters"
    return None


def _visible_item(store: Store, item_id: int, principal: Principal):
    try:
        return library.get(store, item_id, **_vis(principal)), None
    except NotFoundError:
        return None, _flat(404, f"item {item_id} not found", "not_found")


def _read(store: Store, item_id: int):
    with store.reader() as conn:
        found = sources.get_with_meta(conn, item_id)
    if found is None:
        return _flat(404, f"item {item_id} has no sources", "no_sources")
    manifest, updated_at = found
    return {"item_id": item_id, "sources": manifest, "updated_at": updated_at}


@router.put("/{item_id}/sources")
def put_sources(
    item_id: int,
    body: Any = Body(...),
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
):
    check(principal, "write")
    item, missing = _visible_item(store, item_id, principal)
    if missing is not None:
        return missing
    check(principal, "write", kind=item.kind)
    problem = validate_manifest(body)
    if problem is not None:
        return _invalid(problem)
    with store.connect() as conn:
        sources.put(conn, item_id, body)
    return _read(store, item_id)


@router.get("/{item_id}/sources")
def get_sources(
    item_id: int,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
):
    check(principal, "read")
    _, missing = _visible_item(store, item_id, principal)
    if missing is not None:
        return missing
    return _read(store, item_id)
