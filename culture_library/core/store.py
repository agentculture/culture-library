"""Postgres (psycopg 3, sync) + content-addressed file storage under CULTURE_LIBRARY_DATA_DIR.

Cited from agentculture/xteink @ ea2f643 (branch feat/issue-1), path xteink/core/store.py
— cite-don't-import; modified: dropped devices/queue/api_keys tables, widened item kinds,
added visibility/archived_at, blob writing and explicit-id item insertion; later moved from
SQLite (WAL) to Postgres (plan library-on-postgres-and-pgvector, task t1).

The schema lives in numbered SQL files under ``core/migrations/``, applied once each by
:func:`culture_library.core.migrations.migrate` when a :class:`Store` opens; a module that
needs a table ships a new ``NNNN_name.sql`` there instead of registering DDL at import.

psycopg is the optional ``[pg]`` extra and is imported lazily, so ``culture_library.core``
(and the CLI) still import without it; opening a Store without it raises a CoreError.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, Sequence

from . import migrations
from .errors import CoreError, NotFoundError, ValidationError
from .migrations import MigrationError
from .models import KINDS, VISIBILITIES, AddResult, Item

if TYPE_CHECKING:  # pragma: no cover
    import psycopg

DATA_DIR_ENV = "CULTURE_LIBRARY_DATA_DIR"
DEFAULT_DATA_DIR = "~/.local/share/culture-library"

PG_DSN_ENV = "CULTURE_LIBRARY_PG_DSN"
PG_HOST_ENV = "CULTURE_LIBRARY_PG_HOST"
PG_PORT_ENV = "CULTURE_LIBRARY_PG_PORT"
PG_DATABASE_ENV = "CULTURE_LIBRARY_PG_DATABASE"
PG_USER_ENV = "CULTURE_LIBRARY_PG_USER"
# The name of the variable that carries the password, not a password.
PG_PASSWORD_ENV = "CULTURE_LIBRARY_PG_PASSWORD"  # nosec B105
PG_DEFAULTS = {
    PG_HOST_ENV: "127.0.0.1",
    PG_PORT_ENV: "5432",
    PG_DATABASE_ENV: "culture_library",
    PG_USER_ENV: "culture_library",
}
CONNECT_TIMEOUT_S = 10

__all__ = [
    "DATA_DIR_ENV",
    "DatabaseError",
    "MigrationError",
    "Row",
    "Store",
    "conninfo_from_env",
    "now",
]


class DatabaseError(CoreError):
    """The library database cannot be used: driver missing, unreachable, or misconfigured."""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _psycopg() -> Any:
    try:
        import psycopg
    except ImportError as exc:
        raise DatabaseError(
            "the Postgres driver is not installed; install culture-library[pg] "
            "(uv sync --extra pg)"
        ) from exc
    return psycopg


def conninfo_from_env(environ: Mapping[str, str] | None = None) -> str:
    """The libpq conninfo for the library database, from ``CULTURE_LIBRARY_PG_*`` settings.

    ``CULTURE_LIBRARY_PG_DSN``, when set, replaces host, port, database and user. The
    password always comes from ``CULTURE_LIBRARY_PG_PASSWORD`` unless the DSN carries one.
    """
    env = os.environ if environ is None else environ
    conninfo = _psycopg().conninfo
    from_env = env.get(PG_PASSWORD_ENV) or None  # the password, if set
    dsn = env.get(PG_DSN_ENV)
    if dsn:
        if from_env is None or "password" in conninfo.conninfo_to_dict(dsn):
            return dsn
        return conninfo.make_conninfo(dsn, password=from_env)
    parts = {
        "host": env.get(PG_HOST_ENV) or PG_DEFAULTS[PG_HOST_ENV],
        "port": env.get(PG_PORT_ENV) or PG_DEFAULTS[PG_PORT_ENV],
        "dbname": env.get(PG_DATABASE_ENV) or PG_DEFAULTS[PG_DATABASE_ENV],
        "user": env.get(PG_USER_ENV) or PG_DEFAULTS[PG_USER_ENV],
    }
    if from_env is not None:
        parts["password"] = from_env
    return conninfo.make_conninfo("", **parts)


class Row(tuple):
    """A result row readable by index, slice and (case-insensitive) column name.

    It behaves like :class:`sqlite3.Row` for the callers written against it: ``row[0]``,
    ``row["title"]``, ``row.keys()``, ``dict(row)`` and tuple unpacking all work. Being a
    tuple, it also compares equal to a plain tuple of the same values.
    """

    __slots__ = ()
    _names: tuple[str, ...] = ()
    _index: dict[str, int] = {}

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, str):
            try:
                pos = self._index[key]
            except KeyError:
                pos = self._index.get(key.casefold(), -1)
                if pos < 0:
                    raise IndexError(f"no such column: {key}") from None
            return tuple.__getitem__(self, pos)
        return tuple.__getitem__(self, key)

    def keys(self) -> list[str]:
        return list(self._names)

    def __repr__(self) -> str:
        pairs = ", ".join(f"{n}={v!r}" for n, v in zip(self._names, self))
        return f"<Row {pairs}>"


@lru_cache(maxsize=512)
def _row_class(names: tuple[str, ...]) -> type[Row]:
    index: dict[str, int] = {}
    for pos, name in enumerate(names):  # the first of a repeated name wins, as in sqlite3
        index.setdefault(name, pos)
    for pos, name in enumerate(names):
        index.setdefault(name.casefold(), pos)
    return type("Row", (Row,), {"__slots__": (), "_names": names, "_index": index})


def row_factory(cursor: Any) -> Callable[[Sequence[Any]], Row]:
    """psycopg row factory producing :class:`Row`."""
    description = cursor.description
    cls = _row_class(tuple(col.name for col in description) if description else ())

    def make(values: Sequence[Any]) -> Row:
        return cls(values)

    return make


# The checks a database must pass before the Store migrates or uses it.
_COLLATION_SQL = """
SELECT datlocprovider, datcollate, datlocale
FROM pg_database WHERE datname = current_database()
"""


def _check_collation(conn: Any) -> None:
    provider, collate, locale = conn.execute(_COLLATION_SQL).fetchone()
    if provider == "c" and collate in ("C", "POSIX"):
        return
    if provider == "b" and locale == "C":
        return
    if provider == "c":
        shown = collate
    else:
        shown = f"{locale} ({'icu' if provider == 'i' else provider})"
    raise DatabaseError(
        f"the library database uses the {shown} collation, not C, so text ORDER BY would "
        "differ from the library's binary ordering. Recreate it with: CREATE DATABASE <name> "
        "TEMPLATE template0 LOCALE_PROVIDER libc LC_COLLATE 'C' LC_CTYPE 'C' (then restore)"
    )


class Store:
    """Owns the data directory (``files/<aa>/<sha256>``) and the library's Postgres database.

    ``dsn`` is a libpq conninfo or URL; without it the ``CULTURE_LIBRARY_PG_*`` settings
    are read (see :func:`conninfo_from_env`). Opening checks the database's collation and
    applies pending migrations; a current database runs no DDL.
    """

    def __init__(
        self,
        data_dir: str | os.PathLike[str] | None = None,
        dsn: str | None = None,
    ) -> None:
        raw = data_dir or os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR
        self.data_dir = Path(raw).expanduser()
        self.files_dir = self.data_dir / "files"
        self._psycopg = _psycopg()
        self._conninfo = dsn if dsn is not None else conninfo_from_env()
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def __repr__(self) -> str:
        return f"Store(data_dir={str(self.data_dir)!r})"

    def _open(self) -> psycopg.Connection:
        psycopg = self._psycopg
        try:
            return psycopg.connect(
                self._conninfo,
                autocommit=False,
                row_factory=row_factory,
                connect_timeout=CONNECT_TIMEOUT_S,
            )
        except psycopg.OperationalError as exc:
            first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            raise DatabaseError(
                f"cannot connect to the library database ({first}); check the "
                f"CULTURE_LIBRARY_PG_* settings ({PG_DSN_ENV}, or host/port/database/user "
                f"plus {PG_PASSWORD_ENV}) and that Postgres is running"
            ) from exc

    def ensure_schema(self) -> list[str]:
        """Check the database and apply pending migrations; return the names applied."""
        with self.connect() as conn:
            _check_collation(conn)
            return migrations.migrate(conn)

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        """Short-lived connection; one transaction, committed on success, rolled back on error.

        Unlike SQLite's ``BEGIN IMMEDIATE``, this takes no lock up front: concurrent writers
        run in parallel under READ COMMITTED, so a read-then-write that must not race needs
        ``ON CONFLICT``, ``SELECT ... FOR UPDATE`` or an advisory lock.
        """
        conn = self._open()
        try:
            try:
                yield conn
            except BaseException:
                with suppress(Exception):  # never mask the original error
                    conn.rollback()
                raise
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def reader(self) -> Iterator[psycopg.Connection]:
        """Short-lived read-only connection: its transaction is READ ONLY, so it cannot write
        or take row locks (``SELECT ... FOR UPDATE`` raises), and readers never block writers."""
        conn = self._open()
        try:
            conn.read_only = True
            yield conn
        finally:
            with suppress(Exception):
                conn.rollback()
            conn.close()

    def blob_path(self, sha256: str) -> Path:
        return self.files_dir / sha256[:2] / sha256

    def write_blob(self, data: bytes) -> str:
        """Store bytes at files/<aa>/<sha256> (idempotent); return the sha256."""
        sha = hashlib.sha256(data).hexdigest()
        path = self.blob_path(sha)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            # Unique per writer: threads share a pid, and equal bytes objects can share an id.
            tmp = path.with_name(f"{sha}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
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
        on_insert: Callable[[Any, Item], None] | None = None,
    ) -> AddResult:
        """Insert an item (dedup on sha256). ``item_id`` preserves explicit ids (migration).

        ``on_insert(conn, item)`` runs inside the insert's transaction, after the insert and
        before the commit (index rows, feed events); if it raises, the insert rolls back. It
        is not called when the bytes were already stored (``created`` is False). Concurrent
        adds of the same bytes create one item: the loser's insert hits ``ON CONFLICT``.
        """
        if kind not in KINDS:
            raise ValidationError(f"kind must be one of {', '.join(KINDS)}")
        if not title.strip():
            raise ValidationError("title is required")
        if visibility is not None and visibility not in VISIBILITIES:
            raise ValidationError(f"visibility must be one of {', '.join(VISIBILITIES)}")
        sha = self.write_blob(data)
        with self.connect() as conn:
            # Look first so a duplicate upload does not consume an identity value.
            row = conn.execute("SELECT * FROM items WHERE sha256 = %s", (sha,)).fetchone()
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
            marks = ", ".join("%s" for _ in cols)
            names = ", ".join(cols)  # fixed column names above; values are bound parameters
            insert = f"INSERT INTO items ({names}) VALUES ({marks})"  # nosec B608
            row = conn.execute(
                insert + " ON CONFLICT (sha256) DO NOTHING RETURNING *", vals
            ).fetchone()
            if row is None:  # a concurrent add of the same bytes committed first
                row = conn.execute("SELECT * FROM items WHERE sha256 = %s", (sha,)).fetchone()
                return AddResult(Item(**dict(row)), False)
            if item_id is not None:
                _advance_items_id(conn, item_id)
            item = Item(**dict(row))
            if on_insert is not None:
                on_insert(conn, item)
            return AddResult(item, True)

    def get_item(self, item_id: int) -> Item | None:
        with self.reader() as conn:
            row = conn.execute("SELECT * FROM items WHERE id = %s", (item_id,)).fetchone()
        return Item(**dict(row)) if row else None

    def read_bytes(self, item_id: int) -> bytes:
        item = self.get_item(item_id)
        if item is None:
            raise NotFoundError(f"item {item_id} not found")
        return self.blob_path(item.sha256).read_bytes()


def _advance_items_id(conn: Any, item_id: int) -> None:
    """Move the items identity past an explicit id, never backwards (like AUTOINCREMENT)."""
    conn.execute(
        "SELECT setval(q.seq, %s) FROM"
        " (SELECT pg_get_serial_sequence('items', 'id')::regclass AS seq) q"
        " WHERE %s > COALESCE(pg_sequence_last_value(q.seq), 0)",
        (item_id, item_id),
    )
