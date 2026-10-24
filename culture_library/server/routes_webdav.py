"""``/dav``: a read-only WebDAV view of the library, organised by kind (decision c46).

Tree: ``/dav/`` -> ``/dav/{kind}/`` -> ``/dav/{kind}/{id}-{safe-title}.{ext}``. Supported:
``PROPFIND`` (Depth 0 or 1; absent or ``infinity`` is treated as 1; the request body is
ignored and every answer is allprop, so no client XML is ever parsed), ``GET``, ``HEAD`` and
``OPTIONS``. Every write method (PUT, DELETE, MKCOL, MOVE, COPY, PROPPATCH, LOCK, UNLOCK,
POST, PATCH) answers 405 with ``Allow``; WebDAV stays read-only unless a later decision says
otherwise. Visibility matches the rest of the API: private items only for callers with
``private`` or for the key that uploaded them, archived never. Auth is the same
Bearer-or-Basic-with-key handling as OPDS
(see :mod:`culture_library.server.routes_opds`); ``OPTIONS`` is unauthenticated so clients can
probe the server.
"""

from __future__ import annotations

import re
from email.utils import format_datetime
from urllib.parse import quote
from xml.etree import ElementTree as ET  # nosec B405 - only builds XML, never parses input

from fastapi import APIRouter, Depends, Request, Response

from culture_library.core import KINDS, Item, NotFoundError, Store, library
from culture_library.server.auth import Principal, get_store
from culture_library.server.routes_opds import (
    BasicChallengeRoute,
    file_name,
    kind_summary,
    media_type,
    not_found,
    parse_when,
    reader_principal,
    send_item_file,
    visible_items,
)

DAV_NS = "DAV:"
READ_METHODS = ["OPTIONS", "GET", "HEAD", "PROPFIND"]
WRITE_METHODS = [
    "PUT",
    "DELETE",
    "MKCOL",
    "MOVE",
    "COPY",
    "PROPPATCH",
    "LOCK",
    "UNLOCK",
    "POST",
    "PATCH",
]
ALLOW = ", ".join(READ_METHODS)
MAX_LISTING = 10000
_ID_PREFIX = re.compile(r"^(\d+)-")

router = APIRouter(route_class=BasicChallengeRoute, include_in_schema=False)


def _q(tag: str) -> str:
    return tag  # the default namespace is declared on the root (ET rejects default_namespace)


def _href(*parts: str, collection: bool = False) -> str:
    return "/dav/" + "".join(
        quote(p) + ("/" if collection or i < len(parts) - 1 else "") for i, p in enumerate(parts)
    )


def _http_date(stamp: str | None) -> str | None:
    return format_datetime(parse_when(stamp), usegmt=True) if stamp else None


def _response(
    ms: ET.Element,
    href: str,
    name: str,
    *,
    collection: bool,
    item: Item | None = None,
    modified: str | None = None,
) -> None:
    resp = ET.SubElement(ms, _q("response"))
    ET.SubElement(resp, _q("href")).text = href
    stat = ET.SubElement(resp, _q("propstat"))
    prop = ET.SubElement(stat, _q("prop"))
    ET.SubElement(prop, _q("displayname")).text = name
    rtype = ET.SubElement(prop, _q("resourcetype"))
    if collection:
        ET.SubElement(rtype, _q("collection"))
    if item is not None:
        ET.SubElement(prop, _q("getcontentlength")).text = str(item.size)
        ET.SubElement(prop, _q("getcontenttype")).text = media_type(item.format)
        modified = item.created_at
    date = _http_date(modified)
    if date:
        ET.SubElement(prop, _q("getlastmodified")).text = date
    ET.SubElement(stat, _q("status")).text = "HTTP/1.1 200 OK"


def _multistatus(ms: ET.Element) -> Response:
    body = ET.tostring(ms, encoding="utf-8", xml_declaration=True)
    return Response(body, status_code=207, media_type='application/xml; charset="utf-8"')


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def _resolve_file(store: Store, principal: Principal, kind: str, name: str) -> Item | None:
    match = _ID_PREFIX.match(name)
    if not match or kind not in KINDS:
        return None
    try:
        item = library.get(
            store, int(match.group(1)), include_private=principal.private, owner=principal.key_id
        )
    except NotFoundError:
        return None
    return item if item.kind == kind else None


def _kind_items(store: Store, principal: Principal, kind: str) -> list[Item]:
    return visible_items(
        store, private=principal.private, owner=principal.key_id, kind=kind, limit=MAX_LISTING
    )


@router.api_route("/dav", methods=["OPTIONS"])
@router.api_route("/dav/{path:path}", methods=["OPTIONS"])
def options(path: str = "") -> Response:
    return Response(status_code=200, headers={"DAV": "1", "Allow": ALLOW, "MS-Author-Via": "DAV"})


@router.api_route("/dav", methods=WRITE_METHODS)
@router.api_route("/dav/{path:path}", methods=WRITE_METHODS)
def read_only(path: str = "") -> Response:
    return Response(
        "WebDAV is read-only", status_code=405, media_type="text/plain", headers={"Allow": ALLOW}
    )


def _root_listing(ms: ET.Element, store: Store, principal: Principal, deep: bool) -> None:
    """PROPFIND ``/dav/``: the library collection and (``deep``) one collection per kind."""
    _response(ms, "/dav/", "library", collection=True)
    if deep:
        summary = kind_summary(store, private=principal.private, owner=principal.key_id)
        for kind in KINDS:
            latest = summary[kind][1] if kind in summary else None
            _response(ms, _href(kind, collection=True), kind, collection=True, modified=latest)


def _kind_listing(
    ms: ET.Element, store: Store, principal: Principal, kind: str, deep: bool
) -> None:
    """PROPFIND ``/dav/<kind>/``: the kind's collection and (``deep``) its visible files."""
    _response(ms, _href(kind, collection=True), kind, collection=True)
    if deep:
        for item in _kind_items(store, principal, kind):
            name = file_name(item)
            _response(ms, _href(kind, name), name, collection=False, item=item)


@router.api_route("/dav", methods=["PROPFIND"])
@router.api_route("/dav/{path:path}", methods=["PROPFIND"])
def propfind(
    request: Request,
    path: str = "",
    principal: Principal = Depends(reader_principal),
    store: Store = Depends(get_store),
) -> Response:
    segs = _segments(path)
    deep = request.headers.get("depth", "1").strip() != "0"
    ms = ET.Element("multistatus", {"xmlns": DAV_NS})
    if not segs:
        _root_listing(ms, store, principal, deep)
        return _multistatus(ms)
    kind = segs[0]
    if kind not in KINDS or len(segs) > 2:
        return not_found(f"/dav/{path}")
    if len(segs) == 1:
        _kind_listing(ms, store, principal, kind, deep)
        return _multistatus(ms)
    item = _resolve_file(store, principal, kind, segs[1])
    if item is None:
        return not_found(f"/dav/{path}")
    name = file_name(item)
    _response(ms, _href(kind, name), name, collection=False, item=item)
    return _multistatus(ms)


@router.api_route("/dav", methods=["GET", "HEAD"])
@router.api_route("/dav/{path:path}", methods=["GET", "HEAD"])
def get(
    request: Request,
    path: str = "",
    principal: Principal = Depends(reader_principal),
    store: Store = Depends(get_store),
) -> Response:
    segs = _segments(path)
    if len(segs) == 2:
        item = _resolve_file(store, principal, segs[0], segs[1])
        return send_item_file(store, item) if item else not_found(f"/dav/{path}")
    if len(segs) > 2 or (segs and segs[0] not in KINDS):
        return not_found(f"/dav/{path}")
    if segs:  # a collection: a plain-text listing, for humans poking at it with a browser
        names = [file_name(i) for i in _kind_items(store, principal, segs[0])]
    else:
        names = list(KINDS)
    return Response("\n".join(names) + "\n", media_type="text/plain")
