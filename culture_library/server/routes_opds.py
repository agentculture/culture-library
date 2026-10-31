"""``/opds``: an OPDS 1.2 (Atom) catalog of the library for reading apps.

Layout (all ``GET``, all need the ``read`` scope; private items need ``private`` access or
to have been uploaded by the caller's key, and archived items never appear):

- ``/opds`` navigation feed, one entry per kind that has visible items;
- ``/opds/{kind}?page=N`` acquisition feed, newest first, 50 per page, ``next``/``previous``;
- ``/opds/search?q=`` acquisition feed over title/author (the newest 50 matches), plus
  ``/opds/opensearch.xml``;
- ``/opds/item/{id}/file`` the stored bytes, so acquisition links work for clients that only
  speak HTTP Basic (``/api/library/{id}/file`` takes Bearer only).

Auth for reading apps (decision c46): most OPDS/WebDAV clients only support HTTP Basic, so
:func:`reader_principal` accepts the API key as the Basic *password* (any username) by
rewriting the credential to its Bearer form and delegating to
:func:`culture_library.server.auth.current_principal` (which stays the single place keys are
checked, and stays overridable in tests). A missing/invalid credential answers 401 with
``WWW-Authenticate: Basic realm="culture-library"``. ``routes_webdav`` reuses this module's
helpers.
"""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime
from typing import Any, Callable
from xml.etree import ElementTree as ET  # nosec B405 - only builds XML, never parses input

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute

from culture_library.core import KINDS, Item, NotFoundError, Store, library, uploaders
from culture_library.server.auth import Principal, check, current_principal, get_store

PAGE_SIZE = 50
OPDS_ROOT = "/opds"
REALM = "culture-library"
ATOM_NS = "http://www.w3.org/2005/Atom"  # NOSONAR - an XML namespace name, not a URL fetched
OPDS_CATALOG = "application/atom+xml;profile=opds-catalog"
NAV_TYPE = f"{OPDS_CATALOG};kind=navigation"
ACQ_TYPE = f"{OPDS_CATALOG};kind=acquisition"
REL_ACQ = "http://opds-spec.org/acquisition"  # NOSONAR - an OPDS link relation, not a URL
MEDIA_TYPES = {
    "epub": "application/epub+zip",
    "pdf": "application/pdf",
    "md": "text/markdown",
    "txt": "text/plain",
    "json": "application/json",
}
_UNSAFE = re.compile(r"[^\w .()\-]+")

_COLS = "id, sha256, kind, title, author, format, size, visibility, created_at, archived_at"


# ------------------------------------------------------------------ shared helpers


def media_type(fmt: str) -> str:
    return MEDIA_TYPES.get(fmt, "application/octet-stream")


def safe_title(title: str) -> str:
    """A filename-safe rendering of ``title`` (no path separators, bounded length)."""
    cleaned = re.sub(r"-{2,}", "-", _UNSAFE.sub("-", title)).strip(" .-")
    return cleaned[:80].strip(" .-") or "untitled"


def file_name(item: Item) -> str:
    return f"{item.id}-{safe_title(item.title)}.{item.format}"


def parse_when(stamp: str) -> datetime:
    when = datetime.fromisoformat(stamp)
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def visible_items(
    store: Store,
    *,
    private: bool,
    owner: str | None = None,
    kind: str | None = None,
    limit: int,
    offset: int = 0,
) -> list[Item]:
    """Live items visible to the caller (public, private access, or own uploads), newest first."""
    vis, vis_params = uploaders.visible_sql(private, owner)
    where: list[str] = ["archived_at IS NULL", vis]
    params: list[Any] = list(vis_params)
    if kind is not None:
        where.append("kind = ?")
        params.append(kind)
    cols, cond = _COLS, " AND ".join(where)
    sql = f"SELECT {cols} FROM items WHERE {cond} ORDER BY id DESC LIMIT ? OFFSET ?"  # nosec B608
    with store.reader() as conn:
        rows = conn.execute(sql, (*params, limit, offset)).fetchall()
    return [Item(**{k: r[k] for k in r.keys()}) for r in rows]


def kind_summary(
    store: Store, *, private: bool, owner: str | None = None
) -> dict[str, tuple[int, str]]:
    """``{kind: (count, newest change)}`` for kinds with visible items.

    The newest change is the latest upload or in-place file replace (#10), so a collection's
    date moves when one of its files is replaced under the same id.
    """
    vis, params = uploaders.visible_sql(private, owner)
    # vis is fixed SQL from uploaders.visible_sql; every value is a bound parameter.
    visible = (
        f"SELECT id, kind, created_at FROM items WHERE archived_at IS NULL AND {vis}"  # nosec B608
    )
    sql = (
        f"SELECT kind, count(*) n, max(created_at) latest FROM ({visible})"  # nosec B608
        " GROUP BY kind"
    )
    replaced = (
        f"SELECT v.kind, max(f.at) latest FROM ({visible}) v "  # nosec B608
        "JOIN feed f ON f.item_id = v.id AND f.subject = 'item' "
        "WHERE f.type = 'updated' AND json_extract(f.data, '$.replaced') = 1 GROUP BY v.kind"
    )
    with store.reader() as conn:
        rows = conn.execute(sql, params).fetchall()
        later = dict(conn.execute(replaced, params).fetchall())
    return {r["kind"]: (r["n"], latest_of(r["latest"], later.get(r["kind"]))) for r in rows}


def basic_to_bearer(request: Request) -> None:
    """Rewrite ``Authorization: Basic base64(user:key)`` to ``Bearer key`` in the request."""
    value = request.headers.get("authorization", "")
    scheme, _, rest = value.partition(" ")
    if scheme.lower() != "basic":
        return
    try:
        _, _, supplied_key = base64.b64decode(rest.strip(), validate=True).decode().partition(":")
    except ValueError:  # binascii.Error and UnicodeDecodeError are ValueErrors
        return  # left as Basic: current_principal answers 401
    headers = [(k, v) for k, v in request.scope["headers"] if k != b"authorization"]
    headers.append((b"authorization", f"Bearer {supplied_key}".encode()))
    request.scope["headers"] = headers
    request.__dict__.pop("_headers", None)  # Starlette caches the parsed header view


def reader_principal(
    _: None = Depends(basic_to_bearer), principal: Principal = Depends(current_principal)
) -> Principal:
    """The caller (Bearer or Basic-with-key), who must hold the ``read`` scope."""
    check(principal, "read")
    return principal


class BasicChallengeRoute(APIRoute):
    """Turn the 401 challenge into ``Basic realm="culture-library"`` for reading apps."""

    def get_route_handler(self) -> Callable:
        handler = super().get_route_handler()

        async def route(request: Request) -> Response:
            try:
                return await handler(request)
            except HTTPException as exc:
                if exc.status_code != 401:
                    raise
                raise HTTPException(
                    401, exc.detail, headers={"WWW-Authenticate": f'Basic realm="{REALM}"'}
                ) from None

        return route


def not_found(what: str) -> JSONResponse:
    return JSONResponse({"detail": f"{what} not found", "code": "not_found"}, status_code=404)


def send_item_file(store: Store, item: Item) -> Response:
    path = store.blob_path(item.sha256)
    if not path.exists():
        return not_found(f"item {item.id}")
    return FileResponse(path, media_type=media_type(item.format), filename=file_name(item))


# ------------------------------------------------------------------ Atom building


def _q(tag: str) -> str:
    # Plain tags; the default namespace is declared once on the root (see _feed). ElementTree's
    # default_namespace option rejects the unqualified attribute names (rel, href, ...).
    return tag


def _text(parent: ET.Element, tag: str, text: str) -> ET.Element:
    el = ET.SubElement(parent, _q(tag))
    el.text = text
    return el


def _link(parent: ET.Element, rel: str, href: str, type_: str | None = None, **extra: str) -> None:
    attrs = {"rel": rel, "href": href, **({"type": type_} if type_ else {}), **extra}
    ET.SubElement(parent, _q("link"), attrs)


def _feed(feed_id: str, title: str, updated: str, self_href: str, type_: str, up: str | None):
    feed = ET.Element("feed", {"xmlns": ATOM_NS})
    _text(feed, "id", feed_id)
    _text(feed, "title", title)
    _text(feed, "updated", updated)
    ET.SubElement(_text(feed, "author", ""), _q("name")).text = "culture-library"
    _link(feed, "self", self_href, type_)
    _link(feed, "start", OPDS_ROOT, NAV_TYPE)
    if up:
        _link(feed, "up", up, NAV_TYPE)
    return feed


def _render(feed: ET.Element, type_: str) -> Response:
    body = ET.tostring(feed, encoding="utf-8", xml_declaration=True)
    return Response(body, media_type=type_)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def latest_of(first: str, second: str | None) -> str:
    """The later of two stored stamps, compared as instants (shapes differ: migrated rows
    keep xteink's naive ``YYYY-MM-DD HH:MM:SS``)."""
    if not second:
        return first
    return second if parse_when(second) > parse_when(first) else first


def modified_at(item: Item, changed: dict[int, str]) -> str:
    """When the item's file last changed: its upload, or a later in-place replace (#10)."""
    return latest_of(item.created_at, changed.get(item.id))


def _entry(feed: ET.Element, item: Item, changed: dict[int, str]) -> None:
    entry = ET.SubElement(feed, _q("entry"))
    _text(entry, "title", item.title)
    _text(entry, "id", f"urn:culture-library:item:{item.id}")
    _text(entry, "updated", modified_at(item, changed))
    if item.author:
        ET.SubElement(ET.SubElement(entry, _q("author")), _q("name")).text = item.author
    content = _text(entry, "content", f"{item.kind}, {item.format}, {item.size} bytes")
    content.set("type", "text")
    _link(
        entry,
        REL_ACQ,
        f"/opds/item/{item.id}/file",
        media_type(item.format),
        title=file_name(item),
    )


def _acquisition_feed(
    store: Store,
    items: list[Item],
    *,
    feed_id: str,
    title: str,
    self_href: str,
    links: dict[str, str],
) -> Response:
    changed = library.file_changed_at(store, [i.id for i in items])
    stamps = [modified_at(i, changed) for i in items]
    updated = max(stamps, key=parse_when, default=_now())
    feed = _feed(feed_id, title, updated, self_href, ACQ_TYPE, OPDS_ROOT)
    for rel, href in links.items():
        _link(feed, rel, href, ACQ_TYPE)
    for item in items:
        _entry(feed, item, changed)
    return _render(feed, ACQ_TYPE)


# ------------------------------------------------------------------ routes

router = APIRouter(prefix=OPDS_ROOT, tags=["opds"], route_class=BasicChallengeRoute)


@router.get("")
def root(
    principal: Principal = Depends(reader_principal), store: Store = Depends(get_store)
) -> Response:
    summary = kind_summary(store, private=principal.private, owner=principal.key_id)
    updated = max((latest for _, latest in summary.values()), key=parse_when, default=_now())
    feed = _feed(
        "urn:culture-library:opds:root", "culture-library", updated, OPDS_ROOT, NAV_TYPE, None
    )
    _link(feed, "search", "/opds/opensearch.xml", "application/opensearchdescription+xml")
    for kind in KINDS:
        if kind not in summary:
            continue
        count, latest = summary[kind]
        entry = ET.SubElement(feed, _q("entry"))
        _text(entry, "title", kind)
        _text(entry, "id", f"urn:culture-library:kind:{kind}")
        _text(entry, "updated", latest)
        _text(entry, "content", f"{count} item{'s' if count != 1 else ''}").set("type", "text")
        _link(entry, "subsection", f"/opds/{kind}", ACQ_TYPE)
    return _render(feed, NAV_TYPE)


@router.get("/opensearch.xml")
def opensearch(_: Principal = Depends(reader_principal)) -> Response:
    template = "/opds/search?q={searchTerms}"  # constant: no escaping needed
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">'
        "<ShortName>culture-library</ShortName>"
        "<Description>Search the culture-library catalog by title or author</Description>"
        "<InputEncoding>UTF-8</InputEncoding>"
        f'<Url type="{ACQ_TYPE}" template="{template}"/>'
        "</OpenSearchDescription>"
    )
    return Response(body, media_type="application/opensearchdescription+xml")


@router.get("/search")
def search(
    q: str = Query("", max_length=200),
    principal: Principal = Depends(reader_principal),
    store: Store = Depends(get_store),
) -> Response:
    items = (
        library.list_items(
            store,
            q=q,
            limit=PAGE_SIZE,
            include_private=principal.private,
            owner=principal.key_id,
            newest_first=True,
        )
        if q.strip()
        else []
    )
    return _acquisition_feed(
        store,
        items,
        feed_id="urn:culture-library:opds:search",
        title=f"Search: {q}",
        self_href="/opds/search?" + _qs(q=q),
        links={},
    )


def _qs(**kw: Any) -> str:
    from urllib.parse import urlencode

    return urlencode(kw)


@router.get("/item/{item_id}/file")
def item_file(
    item_id: int,
    principal: Principal = Depends(reader_principal),
    store: Store = Depends(get_store),
) -> Response:
    try:
        item = library.get(
            store, item_id, include_private=principal.private, owner=principal.key_id
        )
    except NotFoundError:
        return not_found(f"item {item_id}")
    return send_item_file(store, item)


@router.get("/{kind}")
def kind_feed(
    kind: str,
    page: int = Query(1, ge=1),
    principal: Principal = Depends(reader_principal),
    store: Store = Depends(get_store),
) -> Response:
    if kind not in KINDS:
        return not_found(f"kind {kind}")
    rows = visible_items(
        store,
        private=principal.private,
        owner=principal.key_id,
        kind=kind,
        limit=PAGE_SIZE + 1,
        offset=(page - 1) * PAGE_SIZE,
    )
    links = {}
    if page > 1:
        links["previous"] = f"/opds/{kind}?page={page - 1}"
    if len(rows) > PAGE_SIZE:
        links["next"] = f"/opds/{kind}?page={page + 1}"
    return _acquisition_feed(
        store,
        rows[:PAGE_SIZE],
        feed_id=f"urn:culture-library:kind:{kind}",
        title=kind,
        self_href=f"/opds/{kind}" + (f"?page={page}" if page > 1 else ""),
        links=links,
    )
