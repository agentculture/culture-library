"""SQLite (WAL) + content-addressed file storage under CULTURE_LIBRARY_DATA_DIR.

Cited from agentculture/xteink @ ea2f643 (branch feat/issue-1), path xteink/core/store.py
— cite-don't-import; modified: dropped devices/queue/api_keys tables, widened item kinds,
added visibility/archived_at, WAL + busy_timeout, IMMEDIATE write transactions, blob
writing, explicit-id item insertion, and a schema-registration hook.

Later modules add their own tables by calling :func:`register_schema` at import time with
idempotent DDL (``CREATE TABLE IF NOT EXISTS ...``); every :class:`Store` applies all
registered fragments when it opens. store.py never needs editing for new tables.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterator

from .errors import ValidationError
from .models import KINDS, VISIBILITIES, AddResult, Item

DATA_DIR_ENV = "CULTURE_LIBRARY_DATA_DIR"
DEFAULT_DATA_DIR = "~/.local/share/culture-library"
BUSY_TIMEOUT_MS = 30_000

_KIND_SQL = ", ".join(f"'{k}'" for k in KINDS)
_VIS_SQL = ", ".join(f"'{v}'" for v in VISIBILITIES)

_BASE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ({_KIND_SQL})),
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    format TEXT NOT NULL,
    size INTEGER NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ({_VIS_SQL})),
    created_at TEXT NOT NULL,
    archived_at TEXT
);
"""

SCHEMA: list[str] = [_BASE_SCHEMA]


def register_schema(ddl: str) -> None:
    """Register an idempotent DDL fragment applied by every Store on open."""
    if ddl not in SCHEMA:
        SCHEMA.append(ddl)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    """Owns the data directory: ``library.db`` plus ``files/<aa>/<sha256>``."""

    def __init__(self, data_dir: str | os.PathLike[str] | None = None) -> None:
        raw = data_dir or os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR
        self.data_dir = Path(raw).expanduser()
        self.files_dir = self.data_dir / "files"
        self.db_path = self.data_dir / "library.db"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def ensure_schema(self) -> None:
        """Apply the base schema and every registered fragment (idempotent)."""
        with self.connect() as conn:
            for ddl in SCHEMA:
                for stmt in ddl.split(";"):
                    if stmt.strip():
                        conn.execute(stmt)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Short-lived connection; one IMMEDIATE transaction, commit on success.

        IMMEDIATE takes the write lock up front (waiting up to busy_timeout), so a
        read-then-write inside the block cannot hit a lock-upgrade 'database is locked'.
        """
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        """Short-lived read-only-use connection (no write lock taken)."""
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    def blob_path(self, sha256: str) -> Path:
        return self.files_dir / sha256[:2] / sha256

    def write_blob(self, data: bytes) -> str:
        """Store bytes at files/<aa>/<sha256> (idempotent); return the sha256."""
        sha = hashlib.sha256(data).hexdigest()
        path = self.blob_path(sha)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{sha}.{os.getpid()}.{id(data)}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return sha

    def add_item(
        self,
        data: bytes,
        *,
        kind: str,
        title: str,
        format: str,
        author: str = "",
        visibility: str | None = None,
        item_id: int | None = None,
        created_at: str | None = None,
        on_insert: Callable[[sqlite3.Connection, Item], None] | None = None,
    ) -> AddResult:
        """Insert an item (dedup on sha256). ``item_id`` preserves explicit ids (migration).

        ``on_insert(conn, item)`` runs inside the insert's transaction, after the insert and
        before the commit (index rows, feed events); if it raises, the insert rolls back. It
        is not called when the bytes were already stored (``created`` is False).
        """
        if kind not in KINDS:
            raise ValidationError(f"kind must be one of {', '.join(KINDS)}")
        if not title.strip():
            raise ValidationError("title is required")
        if visibility is not None and visibility not in VISIBILITIES:
            raise ValidationError(f"visibility must be one of {', '.join(VISIBILITIES)}")
        sha = self.write_blob(data)
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM items WHERE sha256 = ?", (sha,)).fetchone()
            if row is not None:
                return AddResult(Item(**dict(row)), False)
            cols = ["sha256", "kind", "title", "author", "format", "size", "created_at"]
            vals: list = [sha, kind, title, author, format, len(data), created_at or now()]
            if visibility is not None:
                cols.append("visibility")
                vals.append(visibility)
            if item_id is not None:
                cols.append("id")
                vals.append(item_id)
            marks = ", ".join("?" for _ in cols)
            cur = conn.execute(
                f"INSERT INTO items ({', '.join(cols)}) VALUES ({marks})", vals  # nosec B608
            )
            row = conn.execute("SELECT * FROM items WHERE id = ?", (cur.lastrowid,)).fetchone()
            item = Item(**dict(row))
            if on_insert is not None:
                on_insert(conn, item)
            return AddResult(item, True)

    def get_item(self, item_id: int) -> Item | None:
        with self.reader() as conn:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return Item(**dict(row)) if row else None

    def read_bytes(self, item_id: int) -> bytes:
        item = self.get_item(item_id)
        if item is None:
            from .errors import NotFoundError

            raise NotFoundError(f"item {item_id} not found")
        return self.blob_path(item.sha256).read_bytes()
