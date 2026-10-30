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
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterator

from .errors import CoreError, ValidationError
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
MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = []

# The items columns in declaration order; the kind-CHECK rebuild copies exactly these.
_ITEM_COLUMNS = (
    "id",
    "sha256",
    "kind",
    "title",
    "author",
    "format",
    "size",
    "visibility",
    "created_at",
    "archived_at",
)
_KIND_CHECK = f"kind IN ({_KIND_SQL})"
_KIND_CHECK_RE = re.compile(r"kind\s+IN\s*\(([^)]*)\)", re.IGNORECASE)
# Child tables whose rows reference items(id) ON DELETE CASCADE: counted before and after.
_ITEM_CHILDREN = ("embeddings", "index_queue", "item_sources", "eval_sets")


class MigrationError(CoreError):
    """A schema migration's integrity check failed; it was rolled back."""


def _norm(sql: str) -> str:
    return " ".join(sql.split())


def _check_kinds(sql: str) -> list[str] | None:
    """The kinds named by an items table's kind CHECK, or ``None`` if it has none."""
    match = _KIND_CHECK_RE.search(sql)
    if match is None:
        return None
    return [k.strip().strip("'\"") for k in match.group(1).split(",") if k.strip()]


def _copy_items(conn: sqlite3.Connection) -> None:
    """Copy every items row into items_new (a seam so tests can simulate a lossy copy)."""
    cols = ", ".join(_ITEM_COLUMNS)
    conn.execute(f"INSERT INTO items_new ({cols}) SELECT {cols} FROM items")  # nosec B608


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _item_counts(conn: sqlite3.Connection) -> dict[str, object]:
    counts: dict[str, object] = {
        "items": conn.execute("SELECT count(*) FROM items").fetchone()[0],
        "max_id": conn.execute("SELECT max(id) FROM items").fetchone()[0],
    }
    for child in _ITEM_CHILDREN:
        if _table_exists(conn, child):
            counts[child] = conn.execute(
                f"SELECT count(*) FROM {child}"  # nosec B608 - fixed table names
            ).fetchone()[0]
    return counts


def _items_seq(conn: sqlite3.Connection) -> int | None:
    if not _table_exists(conn, "sqlite_sequence"):
        return None
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'items'").fetchone()
    return None if row is None else row[0]


def _rebuild_items(conn: sqlite3.Connection, old_sql: str) -> str:
    """Rebuild items with the current DDL inside the caller's transaction; return a summary.

    Foreign keys must already be OFF on ``conn`` (set before BEGIN, since SQLite ignores the
    pragma inside a transaction); otherwise DROP TABLE items cascades into the child tables.
    """
    before = _item_counts(conn)
    seq = _items_seq(conn)
    new_ddl = _BASE_SCHEMA.replace("CREATE TABLE IF NOT EXISTS items (", "CREATE TABLE items_new (")
    conn.execute(new_ddl.strip().rstrip(";"))
    _copy_items(conn)
    conn.execute("DROP TABLE items")
    conn.execute("ALTER TABLE items_new RENAME TO items")
    # The copy re-derives sqlite_sequence from max(id); put the old high-water mark back so
    # an id that was purged is never reused.
    if seq is not None:
        if _items_seq(conn) is None:
            conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('items', ?)", (seq,))
        else:
            conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'items'", (seq,))
    # Only the tables the rebuild can break; an unrelated old violation must not block it.
    problems = [
        row
        for child in _ITEM_CHILDREN
        if _table_exists(conn, child)
        for row in conn.execute(f"PRAGMA foreign_key_check({child})").fetchall()
    ]
    if problems:
        raise MigrationError(f"items rebuild left foreign-key violations: {problems[:5]}")
    after = _item_counts(conn)
    # A never-used table has no sqlite_sequence row; the rebuild may leave a 0 row instead.
    if after != before or (_items_seq(conn) or 0) != (seq or 0):
        raise MigrationError(
            f"items rebuild changed the data (before {before}, seq {seq}; "
            f"after {after}, seq {_items_seq(conn)}); rolled back"
        )
    old_kinds = _check_kinds(old_sql)
    return (
        f"culture-library: migrated items kind CHECK from "
        f"({', '.join(old_kinds) if old_kinds is not None else '?'}) "
        f"to ({', '.join(KINDS)}); {before['items']} rows copied"
    )


def register_schema(ddl: str) -> None:
    """Register an idempotent DDL fragment applied by every Store on open."""
    if ddl not in SCHEMA:
        SCHEMA.append(ddl)


def register_migration(fn: Callable[[sqlite3.Connection], None]) -> None:
    """Register a step run after the DDL on every open (additive, idempotent, no rebuilds)."""
    if fn not in MIGRATIONS:
        MIGRATIONS.append(fn)


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
        self._migrate_items_kinds()
        with self.connect() as conn:
            for ddl in SCHEMA:
                for stmt in ddl.split(";"):
                    if stmt.strip():
                        conn.execute(stmt)
            for migrate in MIGRATIONS:
                migrate(conn)

    def _migrate_items_kinds(self) -> None:
        """Rebuild an existing items table whose kind CHECK differs from the current KINDS.

        Runs on its own connection, not :meth:`connect`, because ``PRAGMA foreign_keys``
        has to be switched off *before* the transaction begins. The check is repeated after
        the write lock is held, so racing first opens rebuild at most once. Any integrity
        mismatch rolls back and raises, leaving the old table and its CHECK untouched. A
        CHECK naming kinds this release does not know (a newer release's database) is
        refused with :class:`MigrationError`, never narrowed.
        """
        summary = None
        conn = self._open()
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'items'"
                ).fetchone()
                if row is not None and _KIND_CHECK not in _norm(row[0]):
                    unknown = sorted(set(_check_kinds(row[0]) or ()) - set(KINDS))
                    if unknown:  # never narrow a CHECK a newer release widened
                        raise MigrationError(
                            f"library.db allows kinds this release does not know "
                            f"({', '.join(unknown)}); it was written by a newer "
                            "culture-library. Run that release, or restore a snapshot."
                        )
                    summary = _rebuild_items(conn, row[0])
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()
        if summary is not None:
            sys.stderr.write(summary + "\n")

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
