"""In-process cache of stored chunk vectors, kept current by a trigger-fed change log (#25).

Semantic search scores every visible chunk. Reading and unpacking every vector blob from
SQLite on each query cost more than the scoring itself (about 1.4 s for the repo-doc
vectors), so each process keeps the unpacked vectors and their norms in memory, per
database file and table, and loads them once.

Triggers on ``embeddings`` and ``repodoc_embeddings`` append the owner (item or doc id) of
every inserted, updated or deleted chunk to ``vector_log``. Before scoring, :func:`vectors`
reads the log's newest ``seq``. When it is unchanged, nothing is read. When it moved, only
the owners logged since the cache's last ``seq`` are reloaded. When the log no longer holds
that far back (pruned by :func:`prune_log`) or the cache is new, the table is loaded
whole. Row ids are never trusted as a key: a re-embedded item deletes and re-inserts its
chunks, and SQLite may reuse a rowid.

Stdlib only and network-free, like the rest of ``core/``.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from array import array

from .store import register_migration

TABLES = {"embeddings": "item_id", "repodoc_embeddings": "doc_id"}
LOG_KEEP_SECONDS = 86400.0  # a cache that has not refreshed for a day reloads in full

# (owner id, chunk no) -> (vector, norm)
Entry = tuple[array, float]


def _ensure_vector_log(conn: sqlite3.Connection) -> None:
    """Create ``vector_log`` and (re)create its triggers on every open (additive)."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not set(TABLES) <= tables:
        return
    conn.execute(
        "CREATE TABLE IF NOT EXISTS vector_log (seq INTEGER PRIMARY KEY AUTOINCREMENT,"
        " tbl TEXT NOT NULL, owner INTEGER NOT NULL, at REAL NOT NULL)"
    )
    for table, column in TABLES.items():
        for event in ("INSERT", "UPDATE", "DELETE"):
            ref = f"{'OLD' if event == 'DELETE' else 'NEW'}.{column}"
            name = f"{table}_{event.lower()}_vlog"
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            conn.execute(  # table and column names come only from TABLES
                f"CREATE TRIGGER {name} AFTER {event} ON {table} BEGIN"  # nosec B608
                f" INSERT INTO vector_log (tbl, owner, at) VALUES ('{table}', {ref},"
                " (julianday('now') - 2440587.5) * 86400.0); END"
            )


register_migration(_ensure_vector_log)


def prune_log(conn: sqlite3.Connection, *, now: float | None = None) -> int:
    """Drop log rows older than :data:`LOG_KEEP_SECONDS`; returns how many were dropped."""
    cutoff = (time.time() if now is None else now) - LOG_KEEP_SECONDS
    return conn.execute("DELETE FROM vector_log WHERE at < ?", (cutoff,)).rowcount


def _entry(blob: bytes) -> Entry:
    v = array("f")
    v.frombytes(blob)
    return v, math.sqrt(math.sumprod(v, v))


class _TableCache:
    def __init__(self, table: str) -> None:
        self.table = table
        self.owner_col = TABLES[table]
        self.seq: int | None = None
        self.entries: dict[tuple[int, int], Entry] = {}

    def refresh(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT max(seq), min(seq) FROM vector_log WHERE tbl = ?", (self.table,)
        ).fetchone()
        newest, oldest = row if row else (None, None)
        newest = newest or 0
        if self.seq is not None and newest == self.seq:
            return
        if self.seq is None or (oldest is not None and oldest > self.seq + 1):
            self._load_all(conn)
        else:
            self._load_owners(conn, self.seq)
        self.seq = newest

    def _load_all(self, conn: sqlite3.Connection) -> None:
        sql = f"SELECT {self.owner_col}, chunk_no, vec FROM {self.table}"  # nosec B608
        self.entries = {(r[0], r[1]): _entry(r[2]) for r in conn.execute(sql)}

    def _load_owners(self, conn: sqlite3.Connection, after: int) -> None:
        owners = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT owner FROM vector_log WHERE tbl = ? AND seq > ?",
                (self.table, after),
            )
        ]
        gone = set(owners)
        self.entries = {k: v for k, v in self.entries.items() if k[0] not in gone}
        sql = (
            f"SELECT {self.owner_col}, chunk_no, vec FROM {self.table}"  # nosec B608
            f" WHERE {self.owner_col} = ?"
        )
        for owner in owners:
            for r in conn.execute(sql, (owner,)):
                self.entries[(r[0], r[1])] = _entry(r[2])


_lock = threading.Lock()
_caches: dict[tuple[str, str], _TableCache] = {}


def _db_path(conn: sqlite3.Connection) -> str:
    return conn.execute("PRAGMA database_list").fetchone()[2]


def vectors(conn: sqlite3.Connection, table: str) -> dict[tuple[int, int], Entry]:
    """``{(owner id, chunk no): (vector, norm)}`` for ``table``, current as of ``conn``."""
    key = (_db_path(conn), table)
    with _lock:
        cache = _caches.get(key)
        if cache is None:
            cache = _caches[key] = _TableCache(table)
        cache.refresh(conn)
        return cache.entries


def clear() -> None:
    """Forget every cached table (tests; the next query reloads)."""
    with _lock:
        _caches.clear()
