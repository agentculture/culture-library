"""Reader content for the site: item text, rendered repo documents and card excerpts.

New module, written for plan task t15. Same auth and visibility rules as the item and
repo-doc routes: the read scope, and private (or archived) content behaves as absent (404 /
omitted) for a caller without ``Principal.private`` (an item's own uploader excepted, d4).

- ``GET /api/library/{id}/text`` -> ``{item_id, kind, format, title, text, sections, reason}``.
  ``text`` is :func:`culture_library.core.ingest.extract_text` output: plain text, never
  markup. For EPUB, ``sections`` is ``[{heading, paragraphs: [str]}]``, split at
  ``h1``-``h3`` by a stdlib parser that keeps character data only (``script``/``style``/
  ``head``/``title`` bodies are dropped, attributes are never read); otherwise ``null``.
  ``reason`` explains an empty text (e.g. ``pdftotext_missing``).
- ``GET /api/repodocs/{id}/rendered?commit=`` -> ``{doc_id, repo, path, kind, format, title,
  html | text}``. A ``.json`` document that parses is rendered by
  :func:`culture_library.render.devague.render`, which escapes every input string, so its
  fragment is the only HTML the site inserts as HTML. Anything else (Markdown, text, JSON
  that does not parse) is returned verbatim as ``text``, for ``textContent``.
- ``GET /api/excerpts?items=1,2&docs=3`` -> ``{items: {id: str}, docs: {id: {title,
  excerpt}}}``: short plain-text excerpts for home cards (at most 200 ids per list).

Errors are flat ``{detail, code}`` like ``/api/library``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from html.parser import HTMLParser
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool

from culture_library.core import NotFoundError, Store, ValidationError, ingest, library
from culture_library.core.models import Item
from culture_library.core.repodocs import RepoDoc, RepoDocs
from culture_library.render import devague
from culture_library.server.auth import Principal, check, current_principal, get_store
from culture_library.server.routes_library import FlatErrorRoute

router = APIRouter(route_class=FlatErrorRoute, tags=["read"])

EXCERPT_CHARS = 240
MAX_EXCERPT_IDS = 200
# Excerpts extract text on the fly only for files this small (and never for PDF).
EXCERPT_EXTRACT_MAX_BYTES = 5 * 1024 * 1024
_NAV_FILE = re.compile(r"(^|/)(nav|toc)\.x?html?$", re.I)
_WS = re.compile(r"\s+")


def _error(status: int, detail: str, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"detail": detail, "code": code})


def _not_found(what: str) -> HTTPException:
    return _error(404, f"{what} not found", "not_found")


# ------------------------------------------------------------------ EPUB sections


class _SectionParser(HTMLParser):
    """Character data of an XHTML chapter, grouped into sections at ``h1``-``h3``.

    Attributes are ignored and the bodies of ``script``/``style``/``head``/``title`` (and
    inline ``svg``/``math``) are dropped, so nothing an item carries can become markup,
    a URL or code: the output is a list of plain strings.
    """

    _SKIP = {"script", "style", "head", "title", "svg", "math", "noscript", "template"}
    _HEAD = {"h1", "h2", "h3"}
    _BLOCK = {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "td",
        "th",
        "dt",
        "dd",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "blockquote",
        "pre",
        "figcaption",
        "hr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: list[dict[str, Any]] = []
        self._skip = 0
        self._in_head = False
        self._buf: list[str] = []
        self._start("")

    def _start(self, heading: str) -> None:
        self.sections.append({"heading": heading, "paragraphs": []})

    def _flush(self) -> None:
        text = _WS.sub(" ", "".join(self._buf)).strip()
        self._buf = []
        if not text:
            return
        if self._in_head:
            cur = self.sections[-1]
            if cur["paragraphs"] or cur["heading"]:
                self._start(text)
            else:
                cur["heading"] = text
        else:
            self.sections[-1]["paragraphs"].append(text)

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif not self._skip and (tag in self._HEAD or tag in self._BLOCK):
            self._flush()
            if tag in self._HEAD:
                self._in_head = True

    def handle_startendtag(self, tag, attrs):
        if not self._skip and tag in self._BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif not self._skip and (tag in self._HEAD or tag in self._BLOCK):
            self._flush()
            if tag in self._HEAD:
                self._in_head = False

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def epub_sections(data: bytes, limits: ingest.IngestLimits = ingest.DEFAULT_LIMITS) -> list[dict]:
    """``[{heading, paragraphs}]`` for an EPUB, one or more per chapter file, nav/toc skipped.

    Uses ingest's EPUB opener and capped member reader, so the zip-bomb limits that guard
    ingest guard this too.
    """
    zf = ingest._open_epub(data, limits)  # noqa: SLF001 - same caps as ingest
    if zf is None:
        raise ValidationError("not a valid EPUB", "bad_magic")
    budget = limits.max_uncompressed_bytes
    out: list[dict] = []
    with zf:
        names = sorted(
            (
                i
                for i in zf.infolist()
                if i.filename.lower().endswith((".xhtml", ".html", ".htm"))
                and not _NAV_FILE.search(i.filename)
            ),
            key=lambda i: i.filename,
        )
        for info in names:
            raw = ingest._read_member(zf, info, budget)  # noqa: SLF001
            budget -= len(raw)
            parser = _SectionParser()
            parser.feed(raw.decode("utf-8", errors="replace"))
            parser.close()
            out.extend(s for s in parser.sections if s["paragraphs"])
    return out


# ------------------------------------------------------------------ items


def _visible_item(store: Store, item_id: int, principal: Principal) -> Item:
    try:
        return library.get(
            store, item_id, include_private=principal.private, owner=principal.key_id
        )
    except NotFoundError:
        raise _not_found(f"item {item_id}") from None


def _item_text(store: Store, item: Item) -> dict:
    data = store.blob_path(item.sha256).read_bytes()
    try:
        extraction = ingest.extract_text_ex(data, item.format)
        sections = epub_sections(data) if item.format == "epub" else None
    except ValidationError as exc:
        return {"text": "", "sections": None, "reason": exc.code}
    return {"text": extraction.text, "sections": sections, "reason": extraction.reason}


@router.get("/api/library/{item_id}/text")
async def item_text(
    item_id: int,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
) -> dict:
    """Extracted plain text of one item (and EPUB sections) for the reader."""
    check(principal, "read")
    item = _visible_item(store, item_id, principal)
    if not store.blob_path(item.sha256).is_file():
        raise _error(404, f"item {item_id} has no stored file", "file_missing")
    body = await run_in_threadpool(_item_text, store, item)
    return {
        "item_id": item.id,
        "kind": item.kind,
        "format": item.format,
        "title": item.title,
        **body,
    }


# ------------------------------------------------------------------ repo documents


def _md_title(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or None
    return None


def _json_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _doc_content(docs: RepoDocs, doc_id: int, commit: str | None, principal: Principal) -> str:
    try:
        data = docs.content(doc_id, commit, include_private=principal.private)
    except ValidationError as exc:
        raise _error(400, str(exc), "invalid") from None
    except NotFoundError:
        raise _not_found(f"repo doc {doc_id} version") from None
    return data.decode("utf-8", errors="replace")


def _visible_doc(docs: RepoDocs, doc_id: int, principal: Principal) -> RepoDoc:
    doc = docs.get(doc_id, include_private=principal.private)
    if doc is None:
        raise _not_found(f"repo doc {doc_id}")
    return doc


@router.get("/api/repodocs/{doc_id}/rendered")
def repodoc_rendered(
    doc_id: int,
    commit: str | None = None,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
) -> dict:
    """A repo document for the reader: escaped devague HTML for JSON, else plain text."""
    check(principal, "read")
    docs = RepoDocs(store)
    doc = _visible_doc(docs, doc_id, principal)
    text = _doc_content(docs, doc_id, commit, principal)
    head = {"doc_id": doc.id, "repo": doc.repo, "path": doc.path, "kind": doc.kind}
    parsed = _json_or_none(text) if doc.path.lower().endswith(".json") else None
    if parsed is not None:
        title = parsed.get("title") or parsed.get("slug") if isinstance(parsed, dict) else None
        return {
            **head,
            "format": "html",
            "title": str(title) if title else None,
            "html": devague.render(parsed),
        }
    return {**head, "format": "text", "title": _md_title(text), "text": text}


# ------------------------------------------------------------------ excerpts


def _ids(raw: str | None, what: str) -> list[int]:
    if raw is None or not raw.strip():
        return []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) > MAX_EXCERPT_IDS:
        raise _error(400, f"at most {MAX_EXCERPT_IDS} {what} ids per request", "invalid_request")
    if not all(p.isdigit() for p in parts):
        raise _error(400, f"{what} must be a comma-separated list of ids", "invalid_request")
    return list(dict.fromkeys(int(p) for p in parts))


def _clip(text: str, n: int = EXCERPT_CHARS) -> str:
    text = _WS.sub(" ", text).strip()
    if len(text) <= n:
        return text
    cut = text.rfind(" ", 0, n)
    return text[: cut if cut > n // 2 else n].rstrip(" ,.;:") + "…"


def _indexed_body(store: Store, item_id: int) -> str:
    try:
        with store.reader() as conn:
            row = conn.execute("SELECT body FROM search_fts WHERE rowid = ?", (item_id,)).fetchone()
    except sqlite3.Error:  # no search index table yet: fall back to extraction
        return ""
    return row[0] if row and row[0] else ""


def _item_excerpt(store: Store, item: Item) -> str:
    body = _indexed_body(store, item.id)
    if not body and item.format != "pdf" and item.size <= EXCERPT_EXTRACT_MAX_BYTES:
        path = store.blob_path(item.sha256)
        if path.is_file():
            try:
                body = ingest.extract_text(path.read_bytes(), item.format)
            except ValidationError:
                body = ""
    return _clip(body)


def _md_excerpt(text: str) -> str:
    quote = [ln[1:].strip() for ln in text.splitlines() if ln.startswith(">")]
    if quote:
        return _clip(" ".join(quote))
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if block and not block.startswith(("#", "---", "```", "|")):
            return _clip(block)
    return ""


def _json_excerpt(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for claim in devague._dicts(data.get("claims")):  # noqa: SLF001 - tolerant reader
        if claim.get("text"):
            return _clip(str(claim["text"]))
    for task in devague._dicts(data.get("tasks")):  # noqa: SLF001
        if task.get("summary"):
            return _clip(str(task["summary"]))
    return ""


def _doc_excerpt(docs: RepoDocs, doc: RepoDoc, principal: Principal) -> dict | None:
    try:
        raw = docs.content(doc.id, include_private=principal.private)
    except NotFoundError:
        return None
    text = raw.decode("utf-8", errors="replace")
    if doc.path.lower().endswith(".json"):
        data = _json_or_none(text)
        title = None
        if isinstance(data, dict):
            title = data.get("title") or data.get("slug")
        return {"title": str(title) if title else None, "excerpt": _json_excerpt(data)}
    return {"title": _md_title(text), "excerpt": _md_excerpt(text)}


def _excerpts(store: Store, item_ids: list[int], doc_ids: list[int], principal: Principal):
    items: dict[str, str] = {}
    for item_id in item_ids:
        try:
            item = library.get(
                store, item_id, include_private=principal.private, owner=principal.key_id
            )
        except NotFoundError:
            continue
        items[str(item.id)] = _item_excerpt(store, item)
    docs_svc = RepoDocs(store)
    docs: dict[str, dict] = {}
    for doc_id in doc_ids:
        doc = docs_svc.get(doc_id, include_private=principal.private)
        if doc is None:
            continue
        found = _doc_excerpt(docs_svc, doc, principal)
        if found is not None:
            docs[str(doc.id)] = found
    return {"items": items, "docs": docs}


@router.get("/api/excerpts")
async def excerpts(
    request: Request,
    items: str | None = Query(None, description="comma-separated item ids"),
    docs: str | None = Query(None, description="comma-separated repo-doc ids"),
    principal: Principal = Depends(current_principal),
) -> dict:
    """Short plain-text excerpts (and repo-doc titles) for home cards."""
    check(principal, "read")
    item_ids, doc_ids = _ids(items, "items"), _ids(docs, "docs")
    return await run_in_threadpool(_excerpts, get_store(request), item_ids, doc_ids, principal)
