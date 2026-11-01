"""Repo documents: specs, plans and devague records pushed from git working copies.

Identity is ``(repo, path)``. ``repo`` is the normalized remote URL (https, ssh, scp-style
and ``git://`` forms, credentials, a trailing ``.git`` and case all collapse, e.g.
``github.com/agentculture/lobes-cli``), or for a repo without a remote a caller-supplied
``host:/abs/path`` (or ``host:~/path``) identity. Every push pins a version to its commit
sha, so the same ``(repo, path, commit)`` pushed from a clone and from a worktree is one
version. Content that is byte-identical across *different* repos stays two docs (the blob
file is shared, the identities are not).

"current" is the default-branch version (``commit == default_branch_head`` at push time)
with the newest ``committed_at``; a push from a stale clone or a feature branch adds
history but never replaces current.

Visibility fails closed: a push without ``visibility`` is private, a re-push of an existing
version may tighten its visibility (public -> private) but never loosen it, and a doc takes
the visibility of its current version (or its newest version while it has no current).

Working drafts: a push with ``commit="working"`` (no commit metadata needed) upserts the
doc's one row in ``repodoc_drafts`` and never adds a version. Drafts are keyed by doc, i.e. by
``(repo, path)``, never by checkout path, so a clone and its worktrees share one draft. A
draft is *ahead* when its sha256 differs from the doc's newest version (or there is none).
A draft carries its own visibility (the push's, default private); a doc with no versions yet
takes its draft's visibility.

Tombstones: :meth:`RepoDocs.tombstone` marks a doc removed at a commit (or ``working`` for a
never-committed draft), drops its draft and keeps every version readable. Removed docs are
left out of :meth:`RepoDocs.list` unless ``include_removed``. A push that adds something new
(a new version, or a draft) clears the removal; a replayed push of a stored version does not.

Content is bytes end to end (text is UTF-8 encoded), so binary design assets round-trip
byte-identical.

The library only receives pushed content: nothing here (or anywhere in culture_library)
runs git or devague. New, pure stdlib; written for culture-library plan task t8; drafts,
tombstones and the design kind for the devague-work plan task t1. ``push`` and ``tombstone``
take an optional ``on_write(conn, result)`` hook that runs inside their transaction, so a
caller's side rows (the mirror outbox, the feed event) commit or roll back with the push
(deviation d6, task t10).

On Postgres (plan library-on-postgres-and-pgvector, task t11) writers run in parallel under
READ COMMITTED, so every push and tombstone first locks its doc row (``SELECT ... FOR
UPDATE``; a new doc is created with ``INSERT ... ON CONFLICT DO NOTHING``). Writes to one doc
then run one at a time, as SQLite's ``BEGIN IMMEDIATE`` ran them, and end as a serial run
would. The tables come from ``core/migrations/0001_initial.sql``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .errors import NotFoundError, ValidationError
from .models import VISIBILITIES
from .store import Store, now

WORKING = "working"

_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_URL_RE = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<rest>.*)$", re.IGNORECASE)
_SCP_RE = re.compile(r"^(?:[^@/:\s]+@)?(?P<host>[^@/:\s]+):(?P<path>.*)$")
_NET_SCHEMES = {"http", "https", "ssh", "git", "git+ssh", "ssh+git"}

_KIND_PREFIXES = (
    ("docs/design/", "design"),
    ("docs/specs/", "spec"),
    ("docs/plans/", "plan"),
    ("docs/deliveries/", "devague-record"),
    (".devague/", "devague-record"),
)
_DESIGN_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".html",
    ".htm",
    ".pdf",
)


def _clean_segments(path: str, what: str) -> list[str]:
    segs = [s for s in path.replace("\\", "/").split("/") if s not in ("", ".")]
    if not segs or any(s == ".." for s in segs):
        raise ValidationError(f"{what} must be a non-empty path without '..' segments")
    return segs


def repo_filter_id(value: str) -> str:
    """A filter's repo: any remote :func:`normalize_remote` takes, or a repo id as the API
    returns it (``host.tld/org/repo``). Pushes keep the stricter :func:`normalize_remote`."""
    try:
        return normalize_remote(value)
    except ValidationError:
        host, sep, path = (value or "").strip().partition("/")
        if not sep or "." not in host or ":" in host or "@" in host:
            raise
        return _repo_id(host.lower(), path)


def normalize_remote(remote: str) -> str:
    """Map a git remote (or ``host:/abs/path`` for remoteless repos) to one repo id."""
    raw = (remote or "").strip()
    if not raw:
        raise ValidationError("remote is required (a git remote URL, or host:/path)")
    m = _URL_RE.match(raw)
    if m:
        scheme = m.group("scheme").lower()
        if scheme not in _NET_SCHEMES:
            raise ValidationError(
                f"unsupported remote scheme {scheme!r}; for a repo without a remote "
                "pass host:/absolute/path"
            )
        rest = m.group("rest")
        authority, _, path = rest.partition("/")
        host = authority.rsplit("@", 1)[-1].rsplit(":", 1)[0].lower()  # drop creds + port
        return _repo_id(host, path)
    m = _SCP_RE.match(raw)
    if not m:
        raise ValidationError(
            "remote must be a git URL (https/ssh/git@host:org/repo) or host:/absolute/path"
        )
    host, path = m.group("host").lower(), m.group("path").strip()
    if path.startswith(("/", "~")):
        # caller-supplied identity for a repo with no remote: keep host:path verbatim
        lead = "~/" if path.startswith("~") else "/"
        segs = _clean_segments(path.lstrip("~"), "host:path identity")
        return f"{host}:{lead}{'/'.join(segs)}"
    return _repo_id(host, path)


def _repo_id(host: str, path: str) -> str:
    if not host:
        raise ValidationError("remote has no host")
    segs = _clean_segments(path, "remote path")
    if segs[-1].lower().endswith(".git"):
        segs[-1] = segs[-1][:-4]
        if not segs[-1]:
            raise ValidationError("remote path is empty")
    return f"{host}/{'/'.join(segs)}".lower()


def normalize_path(path: str) -> str:
    """Repo-relative POSIX path (leading ./ and / stripped; '..' rejected)."""
    raw = (path or "").strip()
    if raw.replace("\\", "/").endswith("/"):
        raise ValidationError("path must name a file, not a directory")
    return "/".join(_clean_segments(raw, "path"))


def infer_kind(path: str) -> str:
    """design / spec / plan / devague-record from the path; anything else is a document.

    A binary asset (image, HTML, PDF) under any pushed prefix is a design, whatever
    folder it sits in (deviation d1, spec claim c44).
    """
    for prefix, kind in _KIND_PREFIXES:
        if path.startswith(prefix):
            if path.lower().endswith(_DESIGN_SUFFIXES):
                return "design"
            return kind
    return "document"


def _sha(value: str, field: str) -> str:
    sha = (value or "").strip().lower() if isinstance(value, str) else ""
    if not _SHA_RE.match(sha):
        raise ValidationError(f"{field} must be a full 40- or 64-hex commit sha")
    return sha


def _commit_or_working(value: str, field: str = "commit") -> str:
    if isinstance(value, str) and value.strip().lower() == WORKING:
        return WORKING
    return _sha(value, field)


def _instant(value: str) -> str:
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValidationError("committed_at must be an ISO 8601 timestamp") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class RepoDoc:
    id: int
    repo: str
    path: str
    kind: str
    visibility: str
    current_commit: str | None
    created_at: str
    updated_at: str
    removed_at: str | None = None
    removed_commit: str | None = None  # a commit sha, or "working"


@dataclass(frozen=True)
class RepoDocDraft:
    visibility: str
    sha256: str
    size: int
    pushed_at: str
    ahead: bool  # sha256 differs from the newest version's (or the doc has no version)


@dataclass(frozen=True)
class RepoDocVersion:
    commit: str
    committed_at: str
    on_default_branch: bool
    visibility: str
    sha256: str
    size: int
    pushed_at: str


@dataclass(frozen=True)
class PushResult:
    doc: RepoDoc
    version: RepoDocVersion | None  # None for a working push
    created: bool  # False when this (repo, path, commit), or this draft's bytes, was stored
    current_changed: bool
    draft: RepoDocDraft | None = None  # set for a working push


_BY_ID = " WHERE d.id = %s"
_DOC_SELECT = """
SELECT d.id, d.repo, d.path, d.kind, d.visibility, v.commit_sha AS current_commit,
       d.created_at, d.updated_at, d.removed_at, d.removed_commit
FROM repodocs d LEFT JOIN repodoc_versions v ON v.id = d.current_version_id
"""


def _doc(row: Any) -> RepoDoc:
    return RepoDoc(**dict(row))


def _version(row: Any) -> RepoDocVersion:
    return RepoDocVersion(
        commit=row["commit_sha"],
        committed_at=row["committed_at"],
        on_default_branch=bool(row["on_default_branch"]),
        visibility=row["visibility"],
        sha256=row["sha256"],
        size=row["size"],
        pushed_at=row["pushed_at"],
    )


def _content_bytes(content: str | bytes) -> bytes:
    if isinstance(content, str):
        return content.encode("utf-8")
    if isinstance(content, bytes):
        return content
    raise ValidationError("content must be text or bytes")


def _lock_blob(conn: Any, sha: str) -> None:
    """The per-blob transaction advisory lock of ``core/library.py``'s ``lock_blob`` (same key,
    ``'blob:' || sha`` through ``hashtextextended(..., 0)``), inlined because importing
    ``library`` here would be circular. Held until commit, it keeps an unlink of an
    unreferenced blob from deleting the file this push is about to reference."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('blob:' || %s, 0))", (sha,))


def _lock_doc(conn: Any, repo: str, rel: str) -> Any:
    """The doc's ``(id, current_version_id)`` row, locked until this transaction ends."""
    return conn.execute(
        "SELECT id, current_version_id FROM repodocs WHERE repo = %s AND path = %s FOR UPDATE",
        (repo, rel),
    ).fetchone()


def _find_or_create_doc(
    conn: Any, repo: str, rel: str, vis: str, stamp: str
) -> tuple[int, int | None]:
    """``(doc_id, current_version_id before this push)``; creates the doc when new.

    The doc row stays locked until the push commits, so concurrent pushes of one doc run one
    after the other. Looking first keeps a re-push from consuming an identity value; a
    concurrent first push of the same doc makes the insert a no-op, and the second look then
    waits for that push to commit.
    """
    row = _lock_doc(conn, repo, rel)
    if row is not None:
        return row["id"], row["current_version_id"]
    row = conn.execute(
        "INSERT INTO repodocs (repo, path, kind, visibility, created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (repo, path) DO NOTHING RETURNING id",
        (repo, rel, infer_kind(rel), vis, stamp, stamp),
    ).fetchone()
    if row is not None:
        return row["id"], None
    row = _lock_doc(conn, repo, rel)
    return row["id"], row["current_version_id"]


def _touch_version(conn: Any, existing: Any, vis: str, on_default: bool) -> None:
    """Re-push of the same commit: same bytes, so only add the default-branch flag and
    tighten visibility, never loosen it."""
    tighten = vis == "private" and existing["visibility"] != "private"
    conn.execute(
        "UPDATE repodoc_versions SET on_default_branch = GREATEST(on_default_branch, %s),"
        " visibility = CASE WHEN %s THEN 'private' ELSE visibility END"
        " WHERE id = %s",
        (on_default, tighten, existing["id"]),
    )


class RepoDocs:
    """Service over a :class:`Store` for versioned repo documents."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def push(
        self,
        *,
        remote: str,
        path: str,
        commit: str,
        committed_at: str | None = None,
        default_branch_head: str | None = None,
        content: str | bytes,
        visibility: str | None = None,
        on_write: Callable[[Any, PushResult], None] | None = None,
    ) -> PushResult:
        """Store a committed version, or with ``commit="working"`` the doc's working draft.

        ``committed_at`` and ``default_branch_head`` are required for a commit push and
        ignored for a working push. ``on_write(conn, result)``, when given, runs on this
        push's connection inside its transaction (deviation d6), so whatever it writes commits
        or rolls back with the version/draft; if it raises, nothing of the push persists.
        """
        repo = normalize_remote(remote)
        rel = normalize_path(path)
        sha = _commit_or_working(commit)
        if sha != WORKING:
            head = _sha(default_branch_head, "default_branch_head")
            when = _instant(committed_at)
        if visibility is not None and visibility not in VISIBILITIES:
            raise ValidationError(f"visibility must be one of {', '.join(VISIBILITIES)}")
        vis = visibility or "private"
        data = _content_bytes(content)
        blob = self.store.write_blob(data)
        stamp = now()
        with self.store.connect() as conn:
            _lock_blob(conn, blob)  # before the re-check; held until the version row commits
            if not self.store.blob_path(blob).exists():  # a replace/purge cleanup raced us
                self.store.write_blob(data)
            doc_id, old_current = _find_or_create_doc(conn, repo, rel, vis, stamp)
            if sha == WORKING:
                result = self._push_working(conn, doc_id, blob, len(data), vis, stamp)
                if on_write is not None:
                    on_write(conn, result)
                return result
            existing = conn.execute(
                "SELECT * FROM repodoc_versions WHERE doc_id = %s AND commit_sha = %s",
                (doc_id, sha),
            ).fetchone()
            created = existing is None
            on_default = sha == head
            if created:
                conn.execute(
                    "INSERT INTO repodoc_versions (doc_id, commit_sha, committed_at,"
                    " on_default_branch, visibility, sha256, size, pushed_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (doc_id, sha, when, on_default, vis, blob, len(data), stamp),
                )
                self._restore(conn, doc_id)
            else:
                _touch_version(conn, existing, vis, on_default)
            new_current = self._recompute(conn, doc_id, stamp)
            version = conn.execute(
                "SELECT * FROM repodoc_versions WHERE doc_id = %s AND commit_sha = %s",
                (doc_id, sha),
            ).fetchone()
            doc = conn.execute(_DOC_SELECT + _BY_ID, (doc_id,)).fetchone()
            result = PushResult(_doc(doc), _version(version), created, new_current != old_current)
            if on_write is not None:
                on_write(conn, result)
        return result

    def _push_working(
        self, conn: Any, doc_id: int, blob: str, size: int, vis: str, stamp: str
    ) -> PushResult:
        """Upsert the doc's one draft; ``created`` is False when its bytes are unchanged."""
        old = conn.execute(
            "SELECT sha256 FROM repodoc_drafts WHERE doc_id = %s", (doc_id,)
        ).fetchone()
        conn.execute(
            "INSERT INTO repodoc_drafts (doc_id, visibility, sha256, size, pushed_at)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (doc_id) DO UPDATE SET"
            " visibility = excluded.visibility, sha256 = excluded.sha256,"
            " size = excluded.size, pushed_at = excluded.pushed_at",
            (doc_id, vis, blob, size, stamp),
        )
        self._restore(conn, doc_id)
        has_version = conn.execute(
            "SELECT 1 FROM repodoc_versions WHERE doc_id = %s LIMIT 1", (doc_id,)
        ).fetchone()
        if has_version is None:  # a draft-only doc takes its draft's visibility
            conn.execute(
                "UPDATE repodocs SET visibility = %s, updated_at = %s WHERE id = %s",
                (vis, stamp, doc_id),
            )
        else:
            conn.execute("UPDATE repodocs SET updated_at = %s WHERE id = %s", (stamp, doc_id))
        doc = conn.execute(_DOC_SELECT + _BY_ID, (doc_id,)).fetchone()
        draft = self._draft_row(conn, doc_id)
        created = old is None or old["sha256"] != blob
        return PushResult(_doc(doc), None, created, False, draft)

    @staticmethod
    def _restore(conn: Any, doc_id: int) -> None:
        conn.execute(
            "UPDATE repodocs SET removed_at = NULL, removed_commit = NULL WHERE id = %s", (doc_id,)
        )

    @staticmethod
    def _draft_row(conn: Any, doc_id: int) -> RepoDocDraft | None:
        row = conn.execute(
            "SELECT visibility, sha256, size, pushed_at FROM repodoc_drafts WHERE doc_id = %s",
            (doc_id,),
        ).fetchone()
        if row is None:
            return None
        newest = conn.execute(
            "SELECT sha256 FROM repodoc_versions WHERE doc_id = %s"
            " ORDER BY committed_at DESC, id DESC LIMIT 1",
            (doc_id,),
        ).fetchone()
        ahead = newest is None or newest["sha256"] != row["sha256"]
        return RepoDocDraft(
            visibility=row["visibility"],
            sha256=row["sha256"],
            size=row["size"],
            pushed_at=row["pushed_at"],
            ahead=ahead,
        )

    def tombstone(
        self,
        repo: str,
        path: str,
        commit: str,
        *,
        on_write: Callable[[Any, RepoDoc], None] | None = None,
    ) -> RepoDoc:
        """Mark ``(repo, path)`` removed at ``commit`` (a sha, or ``working``).

        Drops the doc's draft; its versions stay stored and readable. The blob file of the
        dropped draft is left in place (other rows may share it). Raises
        :class:`NotFoundError` for a doc the library never received. ``on_write(conn, doc)``
        runs inside the tombstone's transaction, like :meth:`push`'s.
        """
        rid = normalize_remote(repo)
        rel = normalize_path(path)
        sha = _commit_or_working(commit)
        stamp = now()
        with self.store.connect() as conn:
            row = _lock_doc(conn, rid, rel)
            if row is None:
                raise NotFoundError(f"repo doc {rid}:{rel} not found")
            conn.execute("DELETE FROM repodoc_drafts WHERE doc_id = %s", (row["id"],))
            conn.execute(
                "UPDATE repodocs SET removed_at = %s, removed_commit = %s, updated_at = %s"
                " WHERE id = %s",
                (stamp, sha, stamp, row["id"]),
            )
            doc = _doc(conn.execute(_DOC_SELECT + _BY_ID, (row["id"],)).fetchone())
            if on_write is not None:
                on_write(conn, doc)
        return doc

    @staticmethod
    def _recompute(conn: Any, doc_id: int, stamp: str) -> int | None:
        best = conn.execute(
            "SELECT id, visibility FROM repodoc_versions WHERE doc_id = %s AND on_default_branch"
            " ORDER BY committed_at DESC, id DESC LIMIT 1",
            (doc_id,),
        ).fetchone()
        basis = (
            best
            or conn.execute(
                "SELECT id, visibility FROM repodoc_versions WHERE doc_id = %s"
                " ORDER BY committed_at DESC, id DESC LIMIT 1",
                (doc_id,),
            ).fetchone()
        )
        current = best["id"] if best else None
        conn.execute(
            "UPDATE repodocs SET current_version_id = %s, visibility = %s, updated_at = %s"
            " WHERE id = %s",
            (current, basis["visibility"], stamp, doc_id),
        )
        return current

    # --- reads: private docs/versions behave as absent unless include_private ---------

    def get(self, doc_id: int, *, include_private: bool) -> RepoDoc | None:
        with self.store.reader() as conn:
            row = conn.execute(_DOC_SELECT + _BY_ID, (doc_id,)).fetchone()
        if row is None or (row["visibility"] == "private" and not include_private):
            return None
        return _doc(row)

    def find(
        self, remote: str, path: str, *, include_private: bool, include_removed: bool = False
    ) -> RepoDoc | None:
        hits = self.list(
            repo=remote,
            path=path,
            include_private=include_private,
            include_removed=include_removed,
        )
        return hits[0] if hits else None

    def list(
        self,
        *,
        repo: str | None = None,
        path: str | None = None,
        include_private: bool,
        include_removed: bool = False,
    ) -> list[RepoDoc]:
        """Docs in (repo, path) order; removed (tombstoned) docs only with include_removed."""
        where, params = [], []
        if repo is not None:
            where.append("d.repo = %s")
            params.append(repo_filter_id(repo))
        if path is not None:
            where.append("d.path = %s")
            params.append(normalize_path(path))
        if not include_private:
            where.append("d.visibility = 'public'")
        if not include_removed:
            where.append("d.removed_at IS NULL")
        sql = _DOC_SELECT + (" WHERE " + " AND ".join(where) if where else "")
        with self.store.reader() as conn:
            rows = conn.execute(sql + " ORDER BY d.repo, d.path", params).fetchall()
        return [_doc(r) for r in rows]

    def versions(self, doc_id: int, *, include_private: bool) -> list[RepoDocVersion]:
        """Newest first. Empty when the doc is absent or not visible."""
        if self.get(doc_id, include_private=include_private) is None:
            return []
        sql = "SELECT * FROM repodoc_versions WHERE doc_id = %s"
        if not include_private:
            sql += " AND visibility = 'public'"
        with self.store.reader() as conn:
            rows = conn.execute(sql + " ORDER BY committed_at DESC, id DESC", (doc_id,))
            return [_version(r) for r in rows.fetchall()]

    def draft(self, doc_id: int, *, include_private: bool) -> RepoDocDraft | None:
        """The doc's working draft, or None (absent, or private and not include_private)."""
        if self.get(doc_id, include_private=include_private) is None:
            return None
        with self.store.reader() as conn:
            draft = self._draft_row(conn, doc_id)
        if draft is None or (draft.visibility == "private" and not include_private):
            return None
        return draft

    def content(self, doc_id: int, commit: str | None = None, *, include_private: bool) -> bytes:
        """Bytes of ``commit`` (default: current, else the newest visible version).

        ``commit="working"`` returns the working draft's bytes.
        """
        doc = self.get(doc_id, include_private=include_private)
        if doc is None:
            raise NotFoundError(f"repo doc {doc_id} not found")
        if commit is not None and _commit_or_working(commit) == WORKING:
            draft = self.draft(doc_id, include_private=include_private)
            if draft is None:
                raise NotFoundError(f"repo doc {doc_id} has no visible working draft")
            return self.store.blob_path(draft.sha256).read_bytes()
        visible = self.versions(doc_id, include_private=include_private)
        want = _sha(commit, "commit") if commit is not None else doc.current_commit
        if want is None:
            match = visible[0] if visible else None
        else:
            match = next((v for v in visible if v.commit == want), None)
        if match is None:
            raise NotFoundError(f"repo doc {doc_id} has no visible version {want or ''}".strip())
        return self.store.blob_path(match.sha256).read_bytes()


__all__ = [
    "WORKING",
    "PushResult",
    "RepoDoc",
    "RepoDocDraft",
    "RepoDocVersion",
    "RepoDocs",
    "infer_kind",
    "normalize_path",
    "normalize_remote",
]
