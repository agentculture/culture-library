"""Mirror core: storage protocol, readable keys and a durable outbox (plan task t4).

Stdlib only and network-free: the real S3 client lives outside ``core/``. Repo document
versions (and working-tree drafts) are queued in ``mirror_outbox`` and uploaded through a
:class:`MirrorBackend`. The outbox keeps a ``sha256`` column so a backup covers it.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Protocol

from . import repodocs as _repodocs  # noqa: F401  (registers the tables reconcile reads)
from .store import Store, now, register_schema

WORKING = "working"
PENDING = "pending"
DONE = "done"
_MAX_ERROR = 500

register_schema("""
CREATE TABLE IF NOT EXISTS mirror_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    enqueued_at TEXT NOT NULL,
    done_at TEXT
);
CREATE INDEX IF NOT EXISTS mirror_outbox_state ON mirror_outbox (state)
""")


class MirrorBackend(Protocol):
    """What the mirror needs from object storage."""

    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    def head(self, key: str) -> str | None:
        """Return the stored object's sha256, or None when the key is absent."""
        ...


class FakeBackend:
    """In-memory backend for tests; ``fail`` makes put() raise."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.fail: Exception | None = None

    def put(self, key: str, data: bytes, content_type: str) -> None:
        if self.fail is not None:
            raise self.fail
        self.objects[key] = (data, content_type)

    def head(self, key: str) -> str | None:
        import hashlib

        obj = self.objects.get(key)
        return hashlib.sha256(obj[0]).hexdigest() if obj else None


def key_for(repo: str, path: str, commit: str | None) -> str:
    """``<repo>/<path>@<commit>``; a draft (no commit, or ``working``) ends in ``@working``."""
    return f"{repo.strip('/')}/{path.lstrip('/')}@{commit or WORKING}"


def _content_type(path: str) -> str:
    return "text/markdown; charset=utf-8" if path.endswith(".md") else "application/octet-stream"


def enqueue(
    conn: sqlite3.Connection,
    key: str,
    sha256: str,
    content_type: str = "application/octet-stream",
    *,
    force: bool = False,
) -> None:
    """Queue ``key`` on the caller's connection. Same sha already queued/done is a no-op
    unless ``force`` (the backend lost it); a different sha re-queues the key as pending."""
    row = conn.execute("SELECT sha256 FROM mirror_outbox WHERE key = ?", (key,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO mirror_outbox (key, sha256, content_type, enqueued_at)"
            " VALUES (?, ?, ?, ?)",
            (key, sha256, content_type, now()),
        )
    elif force or row[0] != sha256:
        conn.execute(
            "UPDATE mirror_outbox SET sha256 = ?, content_type = ?, state = 'pending',"
            " attempts = 0, last_error = '', enqueued_at = ?, done_at = NULL WHERE key = ?",
            (sha256, content_type, now(), key),
        )


def drain(store: Store, backend: MirrorBackend) -> dict[str, int]:
    """Upload every pending row; mark done, or record attempts/last_error. Never raises."""
    with store.reader() as conn:
        rows = [
            tuple(r)
            for r in conn.execute(
                "SELECT key, sha256, content_type FROM mirror_outbox"
                " WHERE state = 'pending' ORDER BY id"
            )
        ]
    done = failed = 0
    for key, sha, ctype in rows:
        try:
            data = store.blob_path(sha).read_bytes()
            backend.put(key, data, ctype)
        except Exception as exc:  # noqa: BLE001 - failures are recorded, never raised
            failed += 1
            msg = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR]
            with store.connect() as conn:
                conn.execute(
                    "UPDATE mirror_outbox SET attempts = attempts + 1, last_error = ?"
                    " WHERE key = ?",
                    (msg, key),
                )
        else:
            done += 1
            with store.connect() as conn:
                conn.execute(
                    "UPDATE mirror_outbox SET state = 'done', attempts = attempts + 1,"
                    " last_error = '', done_at = ? WHERE key = ? AND sha256 = ?",
                    (now(), key, sha),
                )
    return {"done": done, "failed": failed}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def reconcile(store: Store, backend: MirrorBackend) -> int:
    """Enqueue every version/draft whose key is missing or whose sha differs; return count."""
    wanted: list[tuple[str, str, str]] = []
    with store.reader() as conn:
        for r in conn.execute(
            "SELECT d.repo, d.path, v.commit_sha, v.sha256 FROM repodoc_versions v"
            " JOIN repodocs d ON d.id = v.doc_id"
        ):
            wanted.append((key_for(r[0], r[1], r[2]), r[3], _content_type(r[1])))
        if _has_table(conn, "repodoc_drafts"):
            for r in conn.execute(
                "SELECT d.repo, d.path, x.sha256 FROM repodoc_drafts x"
                " JOIN repodocs d ON d.id = x.doc_id"
            ):
                wanted.append((key_for(r[0], r[1], None), r[2], _content_type(r[1])))
    missing = [w for w in wanted if backend.head(w[0]) != w[1]]
    if missing:
        with store.connect() as conn:
            for key, sha, ctype in missing:
                enqueue(conn, key, sha, ctype, force=True)
    return len(missing)


def pending_stats(store: Store, at: datetime | None = None) -> dict[str, float | int | None]:
    """``{count, oldest_age_seconds}`` over pending rows (age None when none)."""
    with store.reader() as conn:
        row = conn.execute(
            "SELECT COUNT(*), MIN(enqueued_at) FROM mirror_outbox WHERE state = 'pending'"
        ).fetchone()
    count, oldest = row[0], row[1]
    if not count:
        return {"count": 0, "oldest_age_seconds": None}
    ref = at or datetime.now(UTC)
    age = (ref - datetime.fromisoformat(oldest)).total_seconds()
    return {"count": count, "oldest_age_seconds": max(age, 0.0)}
