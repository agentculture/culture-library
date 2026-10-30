"""Repo-document routes: push versioned specs/plans/devague records, read them back.

``POST /api/repodocs`` body (the pre-t10 shape, ``content`` text plus a commit sha, still
works unchanged):

- ``remote``, ``path``: the doc's identity; any extra field (a checkout path, say) is ignored,
  so a clone and its worktrees push one doc.
- ``commit``: a full commit sha, or ``"working"`` for the working-tree draft.
- ``committed_at``, ``default_branch_head``: required for a commit push, ignored otherwise.
- ``content`` (text) or ``content_b64`` (base64 bytes): exactly one, unless ``tombstone``.
  The decoded size is capped at ``CULTURE_LIBRARY_REPODOC_MAX_BYTES`` (default 16 MiB); over
  it, or a request body too large to hold that much as base64, is a 413 ``too_large`` and
  nothing is stored. Send large or binary files as ``content_b64``.
- ``tombstone``: mark the doc removed at ``commit`` (a sha, or ``working``): it leaves the
  default listing, keeps its versions, drops its draft. Content is ignored. A doc the library
  never received is a 404 ``not_found``; a replay of the same tombstone is a no-op.
- ``visibility``: accepted for compatibility and ignored. Every pushed doc, version and draft
  is stored private (the all-private rule, decision c31).
- ``project``: ``{domains: [str], connected: [remote]}`` from the repo's culture.yaml; the
  latest push wins.
- ``spec_links`` (specs only): ``{pr, merge_commit, code_paths, at_commit, at_committed_at,
  trailer_commits}``, see :class:`culture_library.core.speclinks.SpecLinks`.

A new version, or a draft whose bytes changed, queues one mirror outbox row and appends one
repodoc feed event in the version/draft's own transaction (the ``on_write`` hook of
:class:`RepoDocs`, deviation d6); a tombstone appends one ``archived`` event and queues
nothing (S3 keeps every object). A no-op re-push writes neither. The project and spec links
are written after that transaction, on their own connections (their services open one each),
and are validated up front so a bad value is refused before anything is stored.

``GET /api/repodocs`` filters by ``remote``, ``path``, ``domain``, ``project`` (with
``connected=1`` also its connected projects) and ``include_removed=1``; the filters
intersect. Private docs and versions are visible only to a principal with ``private`` set;
to anyone else they behave as absent (404 / omitted). Errors are flat ``{detail, code}``.

The store comes from :func:`culture_library.server.auth.get_store`. Written for plan task t8;
pushes of devague work for the devague-work plan task t10.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import sqlite3
from dataclasses import asdict
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictInt, StrictStr, field_validator, model_validator

from culture_library.core import NotFoundError, Store, ValidationError, feed, mirror
from culture_library.core.projects import Projects
from culture_library.core.projects import _norm as _project_id
from culture_library.core.repodocs import (
    WORKING,
    PushResult,
    RepoDoc,
    RepoDocs,
    infer_kind,
    normalize_path,
)
from culture_library.core.speclinks import SpecLinks
from culture_library.server.auth import Principal, check, current_principal, get_store
from culture_library.server.routes_library import FlatErrorRoute, max_body

log = logging.getLogger("culture_library.server.routes_repodocs")

router = APIRouter(tags=["repodocs"], route_class=FlatErrorRoute)

MAX_BYTES_ENV = "CULTURE_LIBRARY_REPODOC_MAX_BYTES"
DEFAULT_MAX_BYTES = 16 * 1024 * 1024
_BODY_SLACK = 64 * 1024  # room for the JSON framing and the other fields


def max_content_bytes() -> int:
    """The decoded-content cap: ``$CULTURE_LIBRARY_REPODOC_MAX_BYTES`` or 16 MiB."""
    raw = os.environ.get(MAX_BYTES_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        log.warning(
            "%s=%r is not a positive integer; using %d", MAX_BYTES_ENV, raw, DEFAULT_MAX_BYTES
        )
        return DEFAULT_MAX_BYTES
    return value


def _body_cap() -> int:
    """Request-body cap: the content cap as base64 (4 bytes per 3), plus framing slack."""
    return -(-max_content_bytes() // 3) * 4 + _BODY_SLACK


class ProjectScope(BaseModel):
    domains: list[str] = []
    connected: list[str] = []

    @field_validator("connected")
    @classmethod
    def _remotes(cls, value: list[str]) -> list[str]:
        for remote in value:
            try:
                _project_id(remote)
            except ValidationError as exc:
                raise ValueError(f"connected {remote!r}: {exc}") from None
        return value


class PullRef(BaseModel):
    url: str | None = None
    number: StrictInt | None = None


class SpecLinksBody(BaseModel):
    pr: StrictStr | StrictInt | PullRef | None = None
    merge_commit: str | None = None
    code_paths: list[str] | None = None
    at_commit: str | None = None
    at_committed_at: str | None = None
    trailer_commits: list[str] | None = None

    @field_validator("at_committed_at")
    @classmethod
    def _iso(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("at_committed_at must be an ISO-8601 time") from None
        return value

    @model_validator(mode="after")
    def _code_paths_need_a_commit(self) -> SpecLinksBody:
        if self.code_paths is not None and not (self.at_commit and self.at_committed_at):
            raise ValueError("code_paths needs at_commit and at_committed_at")
        return self


class RepoDocPush(BaseModel):
    remote: str
    path: str
    commit: str
    committed_at: str | None = None
    default_branch_head: str | None = None
    visibility: str | None = None  # ignored: every repo doc is stored private
    content: str | None = None
    content_b64: str | None = None
    tombstone: bool = False
    project: ProjectScope | None = None
    spec_links: SpecLinksBody | None = None

    @model_validator(mode="after")
    def _one_content(self) -> RepoDocPush:
        given = (self.content is not None) + (self.content_b64 is not None)
        if not self.tombstone and given != 1:
            raise ValueError("send exactly one of content or content_b64")
        return self


def get_repodocs(store: Store = Depends(get_store)) -> RepoDocs:
    return RepoDocs(store)


def _bad(exc: ValidationError | str) -> HTTPException:
    return HTTPException(status_code=400, detail={"detail": str(exc), "code": "invalid"})


def _absent(what: str) -> HTTPException:
    return HTTPException(
        status_code=404, detail={"detail": f"{what} not found", "code": "not_found"}
    )


def _too_large(limit: int) -> HTTPException:
    return HTTPException(
        status_code=413,
        detail={"detail": f"content exceeds the {limit}-byte limit", "code": "too_large"},
    )


def _content(body: RepoDocPush) -> bytes:
    if body.content_b64 is not None:
        try:
            data = base64.b64decode(body.content_b64, validate=True)
        except ValueError as exc:  # binascii.Error is a ValueError
            raise _bad("content_b64 is not valid base64") from exc
    else:
        data = (body.content or "").encode("utf-8")
    limit = max_content_bytes()
    if len(data) > limit:
        raise _too_large(limit)
    return data


def _record(conn: sqlite3.Connection, result: PushResult) -> None:
    """Queue the mirror upload and append the feed event, in the push's transaction."""
    if not result.created:  # a replayed version or unchanged draft: nothing new to say
        return
    doc = result.doc
    if result.version is not None:
        commit, sha = result.version.commit, result.version.sha256
    else:
        commit, sha = WORKING, result.draft.sha256
    mirror.enqueue(
        conn, mirror.key_for(doc.repo, doc.path, commit), sha, mirror._content_type(doc.path)
    )
    feed.append_repodoc(
        conn,
        doc.id,
        doc.kind,
        sha,
        {"repo": doc.repo, "path": doc.path, "commit": commit},
        visibility=doc.visibility,
    )


def _record_removal(dropped_draft_sha: str | None):
    def hook(conn: sqlite3.Connection, doc: RepoDoc) -> None:
        newest = conn.execute(
            "SELECT sha256 FROM repodoc_versions WHERE doc_id = ?"
            " ORDER BY committed_at DESC, id DESC LIMIT 1",
            (doc.id,),
        ).fetchone()
        sha = newest[0] if newest else (dropped_draft_sha or "")
        feed.append_repodoc(
            conn,
            doc.id,
            doc.kind,
            sha,
            {"repo": doc.repo, "path": doc.path, "commit": doc.removed_commit},
            type="archived",
            visibility=doc.visibility,
        )

    return hook


def _tombstone(docs: RepoDocs, body: RepoDocPush) -> tuple[RepoDoc, bool]:
    """Tombstone the doc; ``(doc, changed)``. A replay at the same commit changes nothing."""
    try:
        before = docs.find(body.remote, body.path, include_private=True, include_removed=True)
    except ValidationError as exc:
        raise _bad(exc) from exc
    if before is None:
        raise _absent(f"repo doc {body.path}")
    if before.removed_at is not None and before.removed_commit == body.commit.strip().lower():
        return before, False
    draft = docs.draft(before.id, include_private=True)
    try:
        doc = docs.tombstone(
            body.remote,
            body.path,
            body.commit,
            on_write=_record_removal(draft.sha256 if draft else None),
        )
    except ValidationError as exc:
        raise _bad(exc) from exc
    except NotFoundError as exc:
        raise _absent(f"repo doc {body.path}") from exc
    return doc, True


def _scope(store: Store, body: RepoDocPush, doc: RepoDoc) -> dict[str, Any]:
    """Write the project and spec links (after the push's transaction committed)."""
    out: dict[str, Any] = {"project": None, "spec_links": None}
    try:
        if body.project is not None:
            out["project"] = Projects(store).upsert(
                body.remote, body.project.domains, body.project.connected
            )
        if body.spec_links is not None:
            out["spec_links"] = SpecLinks(store).set_links(doc.id, **body.spec_links.model_dump())
    except ValidationError as exc:
        raise _bad(exc) from exc
    return out


@router.post("/api/repodocs")
@max_body(_body_cap)
def push_repodoc(
    body: RepoDocPush,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
) -> JSONResponse:
    try:
        kind = infer_kind(normalize_path(body.path))
    except ValidationError as exc:
        raise _bad(exc) from exc
    check(principal, "write", kind)
    if body.spec_links is not None and (kind != "spec" or body.tombstone):
        raise _bad("spec_links can only be sent with a spec push (docs/specs/), not a tombstone")
    docs = RepoDocs(store)
    if body.tombstone:
        doc, changed = _tombstone(docs, body)
        payload = {
            "doc": asdict(doc),
            "version": None,
            "draft": None,
            "created": False,
            "current_changed": False,
            "tombstoned": changed,
            **_scope(store, body, doc),
        }
        return JSONResponse(payload, status_code=200)
    data = _content(body)
    try:
        result = docs.push(
            remote=body.remote,
            path=body.path,
            commit=body.commit,
            committed_at=body.committed_at,
            default_branch_head=body.default_branch_head,
            content=data,
            visibility="private",
            on_write=_record,
        )
    except ValidationError as exc:
        raise _bad(exc) from exc
    payload = {
        "doc": asdict(result.doc),
        "version": asdict(result.version) if result.version else None,
        "draft": asdict(result.draft) if result.draft else None,
        "created": result.created,
        "current_changed": result.current_changed,
        "tombstoned": False,
        **_scope(store, body, result.doc),
    }
    return JSONResponse(payload, status_code=201 if result.created else 200)


def _wanted_repos(
    store: Store, domain: str | None, project: str | None, connected: bool
) -> set[str] | None:
    """Repo ids the domain/project filters allow (intersected), or None for no filter."""
    if connected and project is None:
        raise _bad("connected=1 needs project=")
    wanted: set[str] | None = None
    projects = Projects(store)
    try:
        if project is not None:
            wanted = set(projects.with_connected(project) if connected else [_project_id(project)])
        if domain is not None:
            in_domain = set(projects.repos_for(domain))
            wanted = in_domain if wanted is None else wanted & in_domain
    except ValidationError as exc:
        raise _bad(exc) from exc
    return wanted


@router.get("/api/repodocs")
def list_repodocs(
    remote: str | None = None,
    path: str | None = None,
    domain: str | None = None,
    project: str | None = None,
    connected: bool = False,
    include_removed: bool = False,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
) -> dict:
    check(principal, "read")
    wanted = _wanted_repos(store, domain, project, connected)
    try:
        found = RepoDocs(store).list(
            repo=remote,
            path=path,
            include_private=principal.private,
            include_removed=include_removed,
        )
    except ValidationError as exc:
        raise _bad(exc) from exc
    if wanted is not None:
        found = [d for d in found if d.repo in wanted]
    return {"docs": [asdict(d) for d in found]}


@router.get("/api/repodocs/{doc_id}")
def get_repodoc(
    doc_id: int,
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
) -> dict:
    """``doc`` and ``versions`` as before, plus ``current`` (the current version or null),
    ``draft`` (``{visibility, sha256, size, pushed_at, ahead}`` or null), ``removed``
    (``{removed, removed_at, removed_commit}``; a removed doc is still returned) and
    ``spec_links`` (:meth:`SpecLinks.links`, an empty structure when none)."""
    check(principal, "read")
    docs = RepoDocs(store)
    doc = docs.get(doc_id, include_private=principal.private)
    if doc is None:
        raise _absent(f"repo doc {doc_id}")
    versions = docs.versions(doc_id, include_private=principal.private)
    draft = docs.draft(doc_id, include_private=principal.private)
    current = next((v for v in versions if v.commit == doc.current_commit), None)
    return {
        "doc": asdict(doc),
        "versions": [asdict(v) for v in versions],
        "current": asdict(current) if current else None,
        "draft": asdict(draft) if draft else None,
        "removed": {
            "removed": doc.removed_at is not None,
            "removed_at": doc.removed_at,
            "removed_commit": doc.removed_commit,
        },
        "spec_links": SpecLinks(store).links(doc_id),
    }


# Types that are active content when opened on this origin: served sandboxed, as a download.
_ACTIVE_TYPES = {"text/html", "application/xhtml+xml", "image/svg+xml"}
_TEXT = "text/plain; charset=utf-8"
_BINARY = "application/octet-stream"


def _served_type(path: str, data: bytes) -> str:
    """The Content-Type for a doc's bytes: images and PDF as themselves, HTML/SVG as their
    own type (neutralised by the caller), Markdown/JSON/text as ``text/plain``, anything
    else binary as ``application/octet-stream``."""
    if path.endswith(".md"):
        return mirror._content_type(path).replace("text/markdown", "text/plain")
    guess = mimetypes.guess_type(path)[0] or ""
    if guess in _ACTIVE_TYPES or guess == "application/pdf" or guess.startswith("image/"):
        return guess
    if guess.startswith("text/") or guess == "application/json":
        return _TEXT
    if guess:
        return _BINARY
    try:  # unknown extension: text if it decodes, else a binary download
        data.decode("utf-8")
    except UnicodeDecodeError:
        return _BINARY
    return _TEXT


def _attachment_name(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    safe = "".join(c if c.isascii() and c.isalnum() or c in "._-" else "_" for c in name)
    return safe or "download"


@router.get("/api/repodocs/{doc_id}/content")
def get_repodoc_content(
    doc_id: int,
    commit: str | None = None,
    version: str | None = None,
    principal: Principal = Depends(current_principal),
    docs: RepoDocs = Depends(get_repodocs),
) -> Response:
    """Bytes of one version: ``commit=`` or ``version=`` (a sha, or ``working`` for the
    draft; ``version`` wins). The Content-Type follows the file; HTML and SVG are served
    with ``Content-Security-Policy: sandbox`` and as an attachment, never as same-origin
    active content."""
    check(principal, "read")
    want = version if version is not None else commit
    doc = docs.get(doc_id, include_private=principal.private)
    try:
        data = docs.content(doc_id, want, include_private=principal.private)
    except ValidationError as exc:
        raise _bad(exc) from exc
    except NotFoundError as exc:
        raise _absent(f"repo doc {doc_id} version") from exc
    path = doc.path if doc else ""
    ctype = _served_type(path, data)
    headers = {"X-Content-Type-Options": "nosniff"}
    if ctype.split(";")[0] in _ACTIVE_TYPES or ctype == _BINARY:
        headers["Content-Security-Policy"] = "sandbox"
        headers["Content-Disposition"] = f'attachment; filename="{_attachment_name(path)}"'
    return Response(data, media_type=ctype, headers=headers)
