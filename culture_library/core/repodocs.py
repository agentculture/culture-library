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

The library only receives pushed content: nothing here (or anywhere in culture_library)
runs git or devague. New, pure stdlib; written for culture-library plan task t8.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from .errors import NotFoundError, ValidationError
from .models import VISIBILITIES
from .store import Store, now, register_schema

_VIS_SQL = ", ".join(f"'{v}'" for v in VISIBILITIES)

register_schema(f"""
CREATE TABLE IF NOT EXISTS repodocs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ({_VIS_SQL})),
    current_version_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (repo, path)
);
CREATE TABLE IF NOT EXISTS repodoc_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL REFERENCES repodocs(id),
    commit_sha TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    on_default_branch INTEGER NOT NULL DEFAULT 0,
    visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ({_VIS_SQL})),
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    pushed_at TEXT NOT NULL,
    UNIQUE (doc_id, commit_sha)
);
CREATE INDEX IF NOT EXISTS repodoc_versions_doc ON repodoc_versions (doc_id);
""")

_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_URL_RE = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<rest>.*)$", re.IGNORECASE)
_SCP_RE = re.compile(r"^(?:[^@/:\s]+@)?(?P<host>[^@/:\s]+):(?P<path>.*)$")
_NET_SCHEMES = {"http", "https", "ssh", "git", "git+ssh", "ssh+git"}

_KIND_PREFIXES = (
    ("docs/specs/", "spec"),
    ("docs/plans/", "plan"),
    ("docs/deliveries/", "devague-record"),
    (".devague/", "devague-record"),
)


def _clean_segments(path: str, what: str) -> list[str]:
    segs = [s for s in path.replace("\\", "/").split("/") if s not in ("", ".")]
    if not segs or any(s == ".." for s in segs):
        raise ValidationError(f"{what} must be a non-empty path without '..' segments")
    return segs


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
    """spec / plan / devague-record from the path; anything else is a plain document."""
    for prefix, kind in _KIND_PREFIXES:
        if path.startswith(prefix):
            return kind
    return "document"


def _sha(value: str, field: str) -> str:
    sha = (value or "").strip().lower() if isinstance(value, str) else ""
    if not _SHA_RE.match(sha):
        raise ValidationError(f"{field} must be a full 40- or 64-hex commit sha")
    return sha


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
    version: RepoDocVersion
    created: bool  # False when this (repo, path, commit) was already stored
    current_changed: bool


_DOC_SELECT = """
SELECT d.id, d.repo, d.path, d.kind, d.visibility, v.commit_sha AS current_commit,
       d.created_at, d.updated_at
FROM repodocs d LEFT JOIN repodoc_versions v ON v.id = d.current_version_id
"""


def _doc(row: sqlite3.Row) -> RepoDoc:
    return RepoDoc(**dict(row))


def _version(row: sqlite3.Row) -> RepoDocVersion:
    return RepoDocVersion(
        commit=row["commit_sha"],
        committed_at=row["committed_at"],
        on_default_branch=bool(row["on_default_branch"]),
        visibility=row["visibility"],
        sha256=row["sha256"],
        size=row["size"],
        pushed_at=row["pushed_at"],
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
        committed_at: str,
        default_branch_head: str,
        content: str | bytes,
        visibility: str | None = None,
    ) -> PushResult:
        repo = normalize_remote(remote)
        rel = normalize_path(path)
        sha = _sha(commit, "commit")
        head = _sha(default_branch_head, "default_branch_head")
        when = _instant(committed_at)
        if visibility is not None and visibility not in VISIBILITIES:
            raise ValidationError(f"visibility must be one of {', '.join(VISIBILITIES)}")
        vis = visibility or "private"
        if isinstance(content, str):
            data = content.encode("utf-8")
        elif isinstance(content, bytes):
            data = content
        else:
            raise ValidationError("content must be text")
        on_default = sha == head
        blob = self.store.write_blob(data)
        stamp = now()
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT id, current_version_id FROM repodocs WHERE repo = ? AND path = ?",
                (repo, rel),
            ).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO repodocs (repo, path, kind, visibility, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (repo, rel, infer_kind(rel), vis, stamp, stamp),
                )
                doc_id, old_current = cur.lastrowid, None
            else:
                doc_id, old_current = row["id"], row["current_version_id"]
            existing = conn.execute(
                "SELECT * FROM repodoc_versions WHERE doc_id = ? AND commit_sha = ?",
                (doc_id, sha),
            ).fetchone()
            created = existing is None
            if created:
                conn.execute(
                    "INSERT INTO repodoc_versions (doc_id, commit_sha, committed_at,"
                    " on_default_branch, visibility, sha256, size, pushed_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (doc_id, sha, when, int(on_default), vis, blob, len(data), stamp),
                )
            else:
                # same commit => same bytes; a re-push may only add the default-branch
                # flag and tighten visibility, never loosen it
                tighten = vis == "private" and existing["visibility"] != "private"
                conn.execute(
                    "UPDATE repodoc_versions SET on_default_branch = MAX(on_default_branch, ?),"
                    " visibility = CASE WHEN ? THEN 'private' ELSE visibility END"
                    " WHERE id = ?",
                    (int(on_default), int(tighten), existing["id"]),
                )
            new_current = self._recompute(conn, doc_id, stamp)
            version = conn.execute(
                "SELECT * FROM repodoc_versions WHERE doc_id = ? AND commit_sha = ?",
                (doc_id, sha),
            ).fetchone()
            doc = conn.execute(_DOC_SELECT + " WHERE d.id = ?", (doc_id,)).fetchone()
        return PushResult(_doc(doc), _version(version), created, new_current != old_current)

    @staticmethod
    def _recompute(conn: sqlite3.Connection, doc_id: int, stamp: str) -> int | None:
        best = conn.execute(
            "SELECT id, visibility FROM repodoc_versions WHERE doc_id = ? AND on_default_branch"
            " ORDER BY committed_at DESC, id DESC LIMIT 1",
            (doc_id,),
        ).fetchone()
        basis = (
            best
            or conn.execute(
                "SELECT id, visibility FROM repodoc_versions WHERE doc_id = ?"
                " ORDER BY committed_at DESC, id DESC LIMIT 1",
                (doc_id,),
            ).fetchone()
        )
        current = best["id"] if best else None
        conn.execute(
            "UPDATE repodocs SET current_version_id = ?, visibility = ?, updated_at = ?"
            " WHERE id = ?",
            (current, basis["visibility"], stamp, doc_id),
        )
        return current

    # --- reads: private docs/versions behave as absent unless include_private ---------

    def get(self, doc_id: int, *, include_private: bool) -> RepoDoc | None:
        with self.store.reader() as conn:
            row = conn.execute(_DOC_SELECT + " WHERE d.id = ?", (doc_id,)).fetchone()
        if row is None or (row["visibility"] == "private" and not include_private):
            return None
        return _doc(row)

    def find(self, remote: str, path: str, *, include_private: bool) -> RepoDoc | None:
        hits = self.list(repo=remote, path=path, include_private=include_private)
        return hits[0] if hits else None

    def list(
        self, *, repo: str | None = None, path: str | None = None, include_private: bool
    ) -> list[RepoDoc]:
        where, params = [], []
        if repo is not None:
            where.append("d.repo = ?")
            params.append(normalize_remote(repo))
        if path is not None:
            where.append("d.path = ?")
            params.append(normalize_path(path))
        if not include_private:
            where.append("d.visibility = 'public'")
        sql = _DOC_SELECT + (" WHERE " + " AND ".join(where) if where else "")
        with self.store.reader() as conn:
            rows = conn.execute(sql + " ORDER BY d.repo, d.path", params).fetchall()
        return [_doc(r) for r in rows]

    def versions(self, doc_id: int, *, include_private: bool) -> list[RepoDocVersion]:
        """Newest first. Empty when the doc is absent or not visible."""
        if self.get(doc_id, include_private=include_private) is None:
            return []
        sql = "SELECT * FROM repodoc_versions WHERE doc_id = ?"
        if not include_private:
            sql += " AND visibility = 'public'"
        with self.store.reader() as conn:
            rows = conn.execute(sql + " ORDER BY committed_at DESC, id DESC", (doc_id,))
            return [_version(r) for r in rows.fetchall()]

    def content(self, doc_id: int, commit: str | None = None, *, include_private: bool) -> bytes:
        """Bytes of ``commit`` (default: current, else the newest visible version)."""
        doc = self.get(doc_id, include_private=include_private)
        if doc is None:
            raise NotFoundError(f"repo doc {doc_id} not found")
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
    "PushResult",
    "RepoDoc",
    "RepoDocVersion",
    "RepoDocs",
    "infer_kind",
    "normalize_path",
    "normalize_remote",
]
