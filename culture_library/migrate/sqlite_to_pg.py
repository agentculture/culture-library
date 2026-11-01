"""One-shot copy of a SQLite library (live 0.19.8's ``library.db``) into an empty Postgres.

``culture-library migrate sqlite-to-postgres --from <library.db>`` runs :func:`copy_library`.
The SQLite file is read with the stdlib :mod:`sqlite3` module, opened read-only
(``mode=ro``) inside one read transaction, so the copy sees one snapshot; it is never
written. The target is the library's Postgres (the ``CULTURE_LIBRARY_PG_*`` settings), which
is migrated first and must hold no library rows (``schema_version`` does not count).

Everything happens in one Postgres transaction, in this order:

1. read the vector space ``(model_id, strategy)`` from the gateway's ``/capabilities``
   (:meth:`GatewayEmbedder.vector_space`; refused when unreachable). No embedding call is
   made: the embedder is built without a key and never called.
2. stream every table with ``COPY`` (text format; binary for the two vector tables), with
   the ids as they are. Every value is checked against its Postgres column before it is
   written; a row that Postgres would reject is a *problem* (see below).
3. move each identity sequence past the copied ids (and past SQLite's AUTOINCREMENT
   high-water mark in ``sqlite_sequence``, so a deleted id, a feed cursor say, is never
   handed out again).
4. verify: per table ``count(*)`` and ``max(id)`` on both sides, and for the full-text and
   vector tables a digest of every row's content (vectors compared as float32 bytes).
5. commit only when there were no problems and every table matched; otherwise roll back,
   so a refused or failed copy leaves the target exactly as it was.

The table mapping (SQLite -> Postgres; see core/migrations/0001_initial.sql):

* every table keeps its name and columns, except ``feed.cursor`` -> ``feed.seq``;
* ``search_fts(rowid, title, body)`` -> ``item_text(item_id, title, body)`` and
  ``repodoc_fts(rowid, title, body, label, visibility)`` -> ``repodoc_text(doc_id, ...)``;
  a NULL FTS5 text column (FTS5 has no NOT NULL) becomes ``''``; ``tsv`` is generated;
* ``embeddings``/``repodoc_embeddings``: the SQLite ``model`` label
  (``gemma2-embed+Retrieval-document``) is replaced by ``model_id`` and ``strategy`` from
  ``/capabilities``; the float32 blob becomes a pgvector ``vector`` with the same float32
  values, bit for bit (written in pgvector's binary format, never through decimal text);
* INTEGER 0/1 flags -> boolean; JSON in TEXT -> jsonb;
* ``vector_log`` (0.19.8's change log for its in-process vector cache) is not copied:
  Postgres answers vector queries from pgvector and keeps no cache. The report names it with
  its row count; a source without it (written before 0.19.8 first opened it) copies too;
* ``repodoc_dirty`` is copied exactly: the plpgsql triggers mark every copied doc while
  ``repodocs`` and its versions and drafts arrive, and those marks are deleted before the
  SQLite marks are copied. (The indexer re-plans every doc once on its first run anyway.)

Problems (plan risk r4) are reported per table and column with the row's key, and refuse
the copy (nothing is written); none is mapped silently:

* jsonb rejects ``NaN``/``Infinity``/``-Infinity`` literals, ``\\u0000`` and unpaired
  surrogates, and text rejects NUL and invalid UTF-8: such values are problems;
* a row whose foreign key names a missing parent (an orphan FTS row, an index job for a
  deleted item) is a problem;
* a vector whose length is not the strategy's dimension, or that holds NaN or an infinity;
* a vector whose ``model`` label is not the gateway embedder's label (it was made by another
  model or prompt). ``drop_stale_vectors=True`` skips those instead; they are counted as
  skipped in the report, and the indexer re-embeds them (``requeue_stale_model``).
"""

from __future__ import annotations

import collections
import hashlib
import json
import math
import os
import sqlite3
import struct
import sys
import time
from array import array
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, Sequence

from culture_library.core import indexer, migrations
from culture_library.core.embedding_strategy import Strategy
from culture_library.core.store import _check_collation, _psycopg

__all__ = [
    "COPY_ORDER",
    "CopyError",
    "Problem",
    "Report",
    "SourceError",
    "TableReport",
    "TargetNotEmptyError",
    "copy_library",
    "embedder_from_env",
    "verify_library",
]

# pg_advisory_xact_lock key serialising copies into one database ("culture-library sqlite").
COPY_LOCK_KEY = 0x6C6962_73716C74

# Target tables in copy order: every parent before its children.
COPY_ORDER = (
    "items",
    "api_keys",
    "feed",
    "item_uploaders",
    "item_sources",
    "repodocs",
    "repodoc_versions",
    "repodoc_drafts",
    "projects",
    "spec_links",
    "mirror_outbox",
    "index_queue",
    "index_meta",
    "repodoc_index_queue",
    "item_text",
    "repodoc_text",
    "embeddings",
    "repodoc_embeddings",
    "eval_sets",
    "eval_samples",
    "eval_runs",
    "eval_run_samples",
    "repodoc_dirty",
)

# SQLite tables the copy reads past, with the reason the report gives.
NOT_COPIED = {
    "vector_log": "0.19.8's vector cache change log; Postgres keeps no cache",
}
# Target table -> its SQLite source, where the name differs.
SOURCE_TABLE = {"item_text": "search_fts", "repodoc_text": "repodoc_fts"}
# (target table, target column) -> SQLite column, where the name differs.
RENAMED = {
    ("feed", "seq"): "cursor",
    ("item_text", "item_id"): "rowid",
    ("repodoc_text", "doc_id"): "rowid",
}
# The column each table reports as max(id); None: the table has no numeric id (index_meta).
ID_COLUMN = {
    "items": "id",
    "api_keys": "id",
    "feed": "seq",
    "item_uploaders": "item_id",
    "item_sources": "item_id",
    "repodocs": "id",
    "repodoc_versions": "id",
    "repodoc_drafts": "id",
    "projects": "id",
    "spec_links": "doc_id",
    "mirror_outbox": "id",
    "index_queue": "item_id",
    "index_meta": None,
    "repodoc_index_queue": "doc_id",
    "item_text": "item_id",
    "repodoc_text": "doc_id",
    "embeddings": "item_id",
    "repodoc_embeddings": "doc_id",
    "eval_sets": "id",
    "eval_samples": "id",
    "eval_runs": "id",
    "eval_run_samples": "run_id",
    "repodoc_dirty": "doc_id",
}
VECTOR_TABLES = {"embeddings": "item_id", "repodoc_embeddings": "doc_id"}
TEXT_TABLES = {"item_text": "item_id", "repodoc_text": "doc_id"}
FTS_SHADOW_SUFFIXES = ("_config", "_content", "_data", "_docsize", "_idx")

MAX_LISTED_PROBLEMS = 500
# maintenance_work_mem for rebuilding the HNSW indexes after the copy (SET LOCAL, serial build,
# so local memory): the build is much slower once the graph outgrows it (live's ~70k 768-d
# vectors need a few hundred MB).
HNSW_BUILD_MEMORY = "1GB"

_INT_RANGE = {"integer": 2**31, "bigint": 2**63}


class CopyError(Exception):
    """The copy could not run (an unreadable source, a database error)."""


class SourceError(CopyError):
    """The SQLite file is missing or is not a library of the shape this copy knows."""


class TargetNotEmptyError(CopyError):
    """The target already holds library rows; the copy only fills an empty database."""


class VectorSpace(Protocol):
    label: str

    def vector_space(self) -> tuple[str, Strategy]: ...  # pragma: no cover


@dataclass
class Problem:
    table: str  # the Postgres table
    column: str  # the Postgres column ("" for a whole row)
    row: str  # the row's key, e.g. "id=3" or "item_id=1,chunk_no=0"
    reason: str


@dataclass
class TableReport:
    table: str
    source_table: str
    source_count: int
    source_max_id: int | None
    target_count: int | None = None
    target_max_id: int | None = None
    skipped: int = 0
    digest_ok: bool | None = None  # full-text and vector tables only

    @property
    def ok(self) -> bool:
        if self.target_count is None:
            return False
        if self.target_count != self.source_count - self.skipped:
            return False
        if self.skipped == 0 and self.target_max_id != self.source_max_id:
            return False
        return self.digest_ok is not False

    def to_dict(self) -> dict:
        out = asdict(self)
        out["ok"] = self.ok
        return out


@dataclass
class Report:
    source: str
    mode: str  # "copy" or "verify"
    status: str = "pending"  # copied | refused | mismatch | verified
    model_id: str = ""
    strategy: str = ""
    tables: list[TableReport] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)  # the first MAX_LISTED_PROBLEMS
    problem_count: int = 0
    labels: dict[str, dict[str, int]] = field(default_factory=dict)
    sequences: dict[str, int] = field(default_factory=dict)  # table -> its next identity value
    not_copied: dict[str, int] = field(default_factory=dict)  # NOT_COPIED table -> its rows
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.problems and bool(self.tables) and all(t.ok for t in self.tables)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "mode": self.mode,
            "status": self.status,
            "ok": self.ok,
            "model_id": self.model_id,
            "strategy": self.strategy,
            "tables": [t.to_dict() for t in self.tables],
            "problems": [asdict(p) for p in self.problems],
            "problem_count": self.problem_count,
            "labels": self.labels,
            "sequences": self.sequences,
            "not_copied": self.not_copied,
            "elapsed_s": round(self.elapsed_s, 3),
        }

    def _table_row(self, t: Any) -> tuple[str, ...]:
        return (
            t.table if t.table == t.source_table else f"{t.table} <- {t.source_table}",
            str(t.source_count),
            _show(t.source_max_id),
            _show(t.target_count),
            _show(t.target_max_id),
            str(t.skipped) if t.skipped else "",
            {None: "", True: "same", False: "DIFFERS"}[t.digest_ok],
            "ok" if t.ok else "DIFF",
        )

    def _problem_lines(self) -> list[str]:
        more = self.problem_count - len(self.problems)
        tail = f", the first {len(self.problems)} listed" if more else ""
        lines = [f"problems ({self.problem_count}{tail}):"]
        for p in self.problems:
            where = f"{p.table}.{p.column}" if p.column else p.table
            lines.append(f"  {where} [{p.row}]: {p.reason}")
        return lines

    def render(self) -> str:
        head = {
            "copied": "COPIED (committed)",
            "refused": "REFUSED (rolled back; the target is unchanged)",
            "mismatch": "MISMATCH (rolled back; the target is unchanged)",
            "verified": "VERIFIED",
        }.get(self.status, self.status.upper())
        if self.mode == "verify" and self.status == "mismatch":
            head = "MISMATCH"
        lines = [f"sqlite-to-postgres {self.mode}: {head}", f"source: {self.source}"]
        if self.model_id:
            lines.append(f"vectors: model_id={self.model_id} strategy={self.strategy}")
        cols = ("table", "src count", "src max", "pg count", "pg max", "skip", "digest", "")
        rows = [cols] + [self._table_row(t) for t in self.tables]
        widths = [max(len(r[i]) for r in rows) for i in range(len(cols))]
        for r in rows:
            lines.append("  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip())
        for table, labels in self.labels.items():
            shown = ", ".join(f"{k or '(empty)'}: {v}" for k, v in sorted(labels.items()))
            lines.append(f"labels in {table}: {shown}")
        if self.sequences:
            seqs = ", ".join(f"{k}={v}" for k, v in sorted(self.sequences.items()))
            lines.append(f"next identity values: {seqs}")
        for table, rows in sorted(self.not_copied.items()):
            lines.append(f"not copied: {table} ({rows} rows; {NOT_COPIED[table]})")
        if self.problems:
            lines.extend(self._problem_lines())
        lines.append(f"elapsed: {self.elapsed_s:.1f} s")
        return "\n".join(lines)


def _show(value: int | None) -> str:
    return "-" if value is None else str(value)


# ------------------------------------------------------------------------------ embedder


def embedder_from_env(url: str | None = None, model: str | None = None) -> Any:
    """The indexer's document embedder as ``start_indexer`` builds it, but without a key.

    Its :attr:`label` is what 0.19.8 stored in ``embeddings.model``; :meth:`vector_space`
    reads ``/capabilities`` (which needs no key). With an empty key it cannot embed.
    """
    from culture_library import gateway

    on = gateway.prompts_enabled()
    return gateway.GatewayEmbedder(
        url or os.environ.get("LOBES_GATEWAY_URL") or gateway.DEFAULT_GATEWAY_URL,
        "",
        model or os.environ.get("LOBES_EMBED_MODEL") or gateway.DEFAULT_EMBED_MODEL,
        prompt_name=gateway.PROMPT_NAMES["document"] if on else None,
        prompts=(
            gateway.PROMPT_NAMES["document"] if on else None,
            gateway.PROMPT_NAMES["query"] if on else None,
        ),
    )


# ------------------------------------------------------------------------------ source


def _decode(raw: bytes) -> str:
    # Invalid UTF-8 survives as lone surrogates, which the checks below report.
    return raw.decode("utf-8", "surrogateescape")


def open_source(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """The SQLite library, read-only, inside one read transaction (one snapshot)."""
    db = Path(path).expanduser()
    if not db.is_file():
        raise SourceError(f"no SQLite library at {db}")
    try:
        conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True, isolation_level=None)
        conn.text_factory = _decode
        conn.execute("BEGIN")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.Error as exc:
        raise SourceError(f"cannot read {db} as SQLite: {exc}") from exc
    return conn


def _source_tables(src: sqlite3.Connection) -> set[str]:
    rows = src.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
    virtual = {n for n, sql in rows if (sql or "").upper().startswith("CREATE VIRTUAL TABLE")}
    out = set()
    for name, _sql in rows:
        if name.startswith("sqlite_"):
            continue
        if any(name == v + s for v in virtual for s in FTS_SHADOW_SUFFIXES):
            continue
        out.add(name)
    return out


def _source_columns(src: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in src.execute(f'PRAGMA table_info("{table}")')]


def _sqlite_sequence(src: sqlite3.Connection) -> dict[str, int]:
    try:
        return {n: int(s) for n, s in src.execute("SELECT name, seq FROM sqlite_sequence")}
    except sqlite3.OperationalError:  # no AUTOINCREMENT table at all
        return {}


# ------------------------------------------------------------------------------ target


@dataclass
class _Column:
    name: str
    type: str  # information_schema data_type, or the udt name for USER-DEFINED (vector)
    nullable: bool


def _target_columns(conn: Any, table: str) -> list[_Column]:
    rows = conn.execute(
        "SELECT column_name, data_type, udt_name, is_nullable FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = %s AND is_generated = 'NEVER'"
        " ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [
        _Column(n, u if t == "USER-DEFINED" else t, nullable == "YES") for n, t, u, nullable in rows
    ]


def _target_tables(conn: Any) -> set[str]:
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
    ).fetchall()
    return {r[0] for r in rows} - {"schema_version"}


def _primary_key(conn: Any, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT a.attname FROM pg_index i"
        " JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)"
        " WHERE i.indrelid = %s::regclass AND i.indisprimary"
        " ORDER BY array_position(i.indkey, a.attnum)",
        (table,),
    ).fetchall()
    return [r[0] for r in rows]


def _foreign_keys(conn: Any) -> dict[tuple[str, str], tuple[str, str]]:
    """(child table, column) -> (parent table, column), single-column keys."""
    rows = conn.execute(
        "SELECT c.conrelid::regclass::text, a.attname, c.confrelid::regclass::text, fa.attname"
        " FROM pg_constraint c"
        " JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]"
        " JOIN pg_attribute fa ON fa.attrelid = c.confrelid AND fa.attnum = c.confkey[1]"
        " WHERE c.contype = 'f' AND array_length(c.conkey, 1) = 1"
        " AND c.connamespace = current_schema()::regnamespace"
    ).fetchall()
    return {(r[0], r[1]): (r[2], r[3]) for r in rows}


def _identities(conn: Any) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND is_identity = 'YES' ORDER BY table_name"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def open_target(conninfo: str) -> Any:
    """Connect, check the collation and apply pending migrations (committed)."""
    psycopg = _psycopg()
    try:
        conn = psycopg.connect(conninfo, autocommit=False, connect_timeout=10)
    except psycopg.OperationalError as exc:
        first = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        raise CopyError(f"cannot connect to the target Postgres ({first})") from exc
    try:
        _check_collation(conn)
        migrations.migrate(conn)
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


# ------------------------------------------------------------------------------ values


class _NonFinite(ValueError):
    pass


def _reject_constant(name: str) -> Any:
    raise _NonFinite(name)


def _bad_string(value: str) -> str | None:
    if "\x00" in value:
        return "NUL character"
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate: invalid UTF-8 in SQLite, or a JSON escape
        return "invalid UTF-8 or an unpaired surrogate"
    return None


def _shown(value: Any) -> str:
    """A row key value, printable whatever bytes the source held."""
    return str(value).encode("utf-8", "backslashreplace").decode("utf-8")


def _walk_json(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _walk_json(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_json(v)


def _json_problem(text: str) -> str | None:
    try:
        parsed = json.loads(text, parse_constant=_reject_constant)
    except _NonFinite as exc:
        return f"{exc} in JSON (jsonb has no NaN or Infinity)"
    except ValueError as exc:
        return f"not valid JSON ({exc})"
    for s in _walk_json(parsed):
        if "\x00" in s:
            return "\\u0000 in a JSON string (jsonb rejects it)"
        if _bad_string(s):
            return "an unpaired surrogate in a JSON string (jsonb rejects it)"
    return None


def _convert_int(col: _Column, value: Any) -> tuple[Any, str | None]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None, f"not an integer: {value!r:.40}"
    if not -_INT_RANGE[col.type] <= value < _INT_RANGE[col.type]:
        return None, f"out of range for {col.type}: {value}"
    return value, None


def _convert_float(value: Any) -> tuple[Any, str | None]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"not a number: {value!r:.40}"
    return float(value), None


def _convert_bool(value: Any) -> tuple[Any, str | None]:
    if value in (0, 1) and not isinstance(value, float):
        return bool(value), None
    return None, f"not a 0/1 flag: {value!r:.40}"


def _convert_text(col: _Column, value: Any) -> tuple[Any, str | None]:
    if not isinstance(value, str):
        return None, f"not text ({type(value).__name__})"
    bad = _bad_string(value)
    if not bad and col.type == "jsonb":
        bad = _json_problem(value)
    if bad:
        return None, bad
    return value, None


def _convert_null(col: _Column, *, fts: bool) -> tuple[Any, str | None]:
    if col.nullable:
        return None, None
    if fts and col.type == "text":
        return "", None  # FTS5 columns have no NOT NULL; NULL text is empty text
    return None, "NULL in a NOT NULL column"


def _convert(col: _Column, value: Any, *, fts: bool) -> tuple[Any, str | None]:
    """``(value for Postgres, problem or None)``."""
    if value is None:
        return _convert_null(col, fts=fts)
    if col.type in ("bigint", "integer"):
        return _convert_int(col, value)
    if col.type == "double precision":
        return _convert_float(value)
    if col.type == "boolean":
        return _convert_bool(value)
    if col.type in ("text", "jsonb"):
        return _convert_text(col, value)
    return None, f"no conversion to {col.type}"  # pragma: no cover - schema drift


_NATIVE_LITTLE = sys.byteorder == "little"


def _vector_value(blob: Any, dimension: int) -> tuple[bytes | None, str | None]:
    """pgvector's binary form of a float32 blob (dims, unused, big-endian floats)."""
    if not isinstance(blob, bytes):
        return None, f"not a float32 blob ({type(blob).__name__})"
    if len(blob) % 4:
        return None, f"blob of {len(blob)} bytes is not float32"
    vals = array("f")
    vals.frombytes(blob)
    if len(vals) != dimension:
        return None, f"{len(vals)} dimensions; the strategy has {dimension}"
    if not all(math.isfinite(v) for v in vals):
        return None, "NaN or infinity in the vector (pgvector rejects it)"
    if _NATIVE_LITTLE:
        vals.byteswap()
    return struct.pack(">HH", len(vals), 0) + vals.tobytes(), None


def _blob_from_pg(data: bytes) -> bytes:
    """The float32 blob (native order, as SQLite stored it) of a binary pgvector value."""
    dim, _unused = struct.unpack(">HH", data[:4])
    vals = array("f")
    vals.frombytes(bytes(data[4 : 4 + 4 * dim]))
    if _NATIVE_LITTLE:
        vals.byteswap()
    return vals.tobytes()


# ------------------------------------------------------------------------------ plan


@dataclass
class _TablePlan:
    table: str
    source: str
    columns: list[_Column]  # written columns, in order
    source_exprs: list[str | None]  # the SQLite expression for each column (None: derived)
    pk: list[str]
    fks: dict[str, tuple[str, str]]  # column -> parent (table, column)

    @property
    def is_vector(self) -> bool:
        return self.table in VECTOR_TABLES


def _check_tables(src: sqlite3.Connection, conn: Any) -> None:
    target = _target_tables(conn)
    unknown_target = target - set(COPY_ORDER)
    if unknown_target:
        raise CopyError(
            "the target has tables this copy does not know: "
            f"{', '.join(sorted(unknown_target))}; this culture-library is newer than its copy"
        )
    expected_source = {SOURCE_TABLE.get(t, t) for t in COPY_ORDER}
    have = _source_tables(src) - set(NOT_COPIED)
    missing, extra = expected_source - have, have - expected_source
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing {', '.join(sorted(missing))}")
        if extra:
            parts.append(f"unknown {', '.join(sorted(extra))}")
        raise SourceError(
            f"the SQLite file is not a 0.19.8 library ({'; '.join(parts)}); "
            "copy from the 0.19.8 library.db"
        )


def _column_exprs(
    table: str, source: str, cols: list[_Column], src_cols: set[str]
) -> tuple[set[str], list[str | None]]:
    """``(source columns used, the SQLite expression per target column)``."""
    used: set[str] = set()
    exprs: list[str | None] = []
    for col in cols:
        if table in VECTOR_TABLES and col.name in ("model_id", "strategy"):
            exprs.append(None)
            continue
        name = RENAMED.get((table, col.name), col.name)
        if name not in src_cols:
            raise SourceError(f"{source} has no column {name} (needed for {table}.{col.name})")
        used.add(name)
        exprs.append(name)
    return used, exprs


def _plan_table(
    src: sqlite3.Connection, conn: Any, table: str, fks: dict[tuple[str, str], tuple[str, str]]
) -> _TablePlan:
    source = SOURCE_TABLE.get(table, table)
    cols = _target_columns(conn, table)
    src_cols = set(_source_columns(src, source))
    if source in SOURCE_TABLE.values():
        src_cols.add("rowid")
    used, exprs = _column_exprs(table, source, cols, src_cols)
    if table in VECTOR_TABLES:
        used.add("model")
    leftover = src_cols - used - ({"rowid"} if source in SOURCE_TABLE.values() else set())
    if leftover:
        raise SourceError(
            f"{source} has columns the copy would drop: {', '.join(sorted(leftover))}"
        )
    table_fks = {c: p for (t, c), p in fks.items() if t == table}
    return _TablePlan(table, source, list(cols), exprs, _primary_key(conn, table), table_fks)


def _not_copied_rows(src: sqlite3.Connection) -> dict[str, int]:
    present = _source_tables(src) & set(NOT_COPIED)
    sql = 'SELECT count(*) FROM "{}"'  # nosec B608 - names from NOT_COPIED
    return {t: src.execute(sql.format(t)).fetchone()[0] for t in sorted(present)}


def _plan(src: sqlite3.Connection, conn: Any) -> list[_TablePlan]:
    _check_tables(src, conn)
    fks = _foreign_keys(conn)
    return [_plan_table(src, conn, table, fks) for table in COPY_ORDER]


def _select(plan: _TablePlan, extra: Sequence[str] = ()) -> str:
    exprs = [e for e in plan.source_exprs if e is not None] + list(extra)
    # A vector table's key includes model_id and strategy, which SQLite does not have.
    order = [RENAMED.get((plan.table, c), c) for c in plan.pk]
    order = [c for c in order if c in plan.source_exprs]
    cols = ", ".join(_ident(e) if e != "rowid" else "rowid" for e in exprs)
    keys = ", ".join(_ident(c) if c != "rowid" else "rowid" for c in order)
    return f'SELECT {cols} FROM "{plan.source}" ORDER BY {keys}'  # nosec B608 - catalog names


def _source_stats(src: sqlite3.Connection, plan: _TablePlan) -> tuple[int, int | None]:
    idc = ID_COLUMN[plan.table]
    count = src.execute(f'SELECT count(*) FROM "{plan.source}"').fetchone()[0]  # nosec B608
    if idc is None:
        return count, None
    name = RENAMED.get((plan.table, idc), idc)
    expr = "rowid" if name == "rowid" else _ident(name)
    return count, src.execute(f'SELECT max({expr}) FROM "{plan.source}"').fetchone()[0]  # nosec


def _target_stats(conn: Any, table: str) -> tuple[int, int | None]:
    idc = ID_COLUMN[table]
    top = "NULL" if idc is None else f"max({_ident(idc)})"
    sql = f"SELECT count(*), {top} FROM {_ident(table)}"  # nosec B608 - catalog names
    row = conn.execute(sql).fetchone()
    return row[0], row[1]


def _parent_keys(src: sqlite3.Connection, plans: list[_TablePlan]) -> dict[tuple[str, str], set]:
    by_table = {p.table: p for p in plans}
    out: dict[tuple[str, str], set] = {}
    for plan in plans:
        for parent in plan.fks.values():
            if parent in out:
                continue
            pplan = by_table[parent[0]]
            name = RENAMED.get(parent, parent[1])
            expr = "rowid" if name == "rowid" else _ident(name)
            out[parent] = {
                r[0] for r in src.execute(f'SELECT {expr} FROM "{pplan.source}"')  # nosec B608
            }
    return out


# ------------------------------------------------------------------------------ copy


class _Run:
    """One copy (or verification): the source, target, vector space and the findings."""

    def __init__(
        self,
        src: sqlite3.Connection,
        conn: Any,
        report: Report,
        model_id: str,
        strategy: Strategy,
        label: str,
        drop_stale_vectors: bool,
        maintenance_work_mem: str = HNSW_BUILD_MEMORY,
    ) -> None:
        self.src = src
        self.conn = conn
        self.report = report
        self.model_id = model_id
        self.strategy = strategy
        self.key = strategy.key()
        self.label = label
        self.drop_stale = drop_stale_vectors
        self.plans = _plan(src, conn)
        report.not_copied = _not_copied_rows(src)
        self.parents = _parent_keys(src, self.plans)
        self.vector_oid = 0
        self.maintenance_work_mem = maintenance_work_mem

    def problem(self, table: str, column: str, row: str, reason: str) -> None:
        self.report.problem_count += 1
        if len(self.report.problems) < MAX_LISTED_PROBLEMS:
            self.report.problems.append(Problem(table, column, row, reason))

    def _stale_label(self, plan: _TablePlan, by_name: dict, key: str) -> bool:
        """Record a stale vector label as a problem; True when the row is bad."""
        if not plan.is_vector or by_name["model"] == self.label:
            return False
        self.problem(
            plan.table,
            "model_id",
            key,
            f"vector label {by_name['model']!r} is not the gateway embedder's "
            f"{self.label!r} (stale: pass --drop-stale-vectors to leave it for the "
            "indexer to re-embed)",
        )
        return True

    def _cell(self, plan: _TablePlan, col: _Column, expr: str, by_name: dict, fts: bool):
        """``(value, problem or None)`` for one source cell."""
        if plan.is_vector and col.name == "vec":
            return _vector_value(by_name["vec"], self.strategy.dimension)
        value, why = _convert(col, by_name[expr], fts=fts)
        if why is None and value is not None and col.name in plan.fks:
            parent = plan.fks[col.name]
            if value not in self.parents[parent]:
                why = f"orphan: no {parent[0]} row with {parent[1]} = {value}"
        return value, why

    def _row(self, plan: _TablePlan, by_name: dict, key: str, fts: bool) -> list[Any] | None:
        bad = self._stale_label(plan, by_name, key)
        out: list[Any] = []
        for col, expr in zip(plan.columns, plan.source_exprs):
            if expr is None:  # a vector table's model_id or strategy
                out.append(self.model_id if col.name == "model_id" else self.key)
                continue
            value, why = self._cell(plan, col, expr, by_name, fts)
            if why is not None:
                self.problem(plan.table, col.name, key, why)
                bad = True
            out.append(value)
        return None if bad else out

    def _rows(self, plan: _TablePlan) -> Iterator[list[Any] | None]:
        """Postgres values per source row (None: not written); records problems."""
        extra = ["model"] if plan.is_vector else []
        names = [e for e in plan.source_exprs if e is not None] + extra
        keyed = [
            (c, names.index(RENAMED.get((plan.table, c), c)))
            for c in plan.pk
            if RENAMED.get((plan.table, c), c) in names  # not a vector's model_id/strategy
        ]
        fts = plan.source in SOURCE_TABLE.values()
        for raw in self.src.execute(_select(plan, extra)):
            key = ",".join(f"{c}={_shown(raw[i])}" for c, i in keyed)
            by_name = dict(zip(names, raw))
            if plan.is_vector and by_name["model"] != self.label and self.drop_stale:
                yield None
                continue
            yield self._row(plan, by_name, key, fts)

    def copy_table(self, plan: _TablePlan) -> None:
        cols = ", ".join(_ident(c.name) for c in plan.columns)
        if plan.is_vector:
            sql = f"COPY {_ident(plan.table)} ({cols}) FROM STDIN (FORMAT BINARY)"
        else:
            sql = f"COPY {_ident(plan.table)} ({cols}) FROM STDIN"
        writing = not self.report.problems
        with self.conn.cursor() as cur:
            if not writing:
                collections.deque(self._rows(plan), maxlen=0)  # validate only
                return
            with cur.copy(sql) as copy:
                if plan.is_vector:
                    copy.set_types([_copy_type(c, self.vector_oid) for c in plan.columns])
                for values in self._rows(plan):
                    if values is None or self.report.problems:
                        continue  # skipped, or validating only from the first problem on
                    copy.write_row(values)

    def copy_all(self) -> None:
        self.vector_oid = _register_vector_dumper(self.conn)
        for table in VECTOR_TABLES:
            indexer.ensure_hnsw_index(self.conn, table, self.model_id, self.strategy)
        # Inserting into an HNSW index row by row is the slow part of a large copy (minutes for
        # 70k distinct vectors); one build afterwards is far quicker. Same transaction, so a
        # failed copy restores the indexes with everything else.
        hnsw = _hnsw_indexes(self.conn)
        for name, _definition in hnsw:
            self.conn.execute(f"DROP INDEX {_ident(name)}")
        for plan in self.plans:
            if plan.table == "repodoc_dirty" and not self.report.problems:
                # The triggers marked every copied doc; keep exactly the SQLite marks.
                self.conn.execute("DELETE FROM repodoc_dirty")
            self.copy_table(plan)
        if self.report.problems:
            return  # rolled back by the caller, indexes included
        # A serial build: a parallel one puts the graph in dynamic shared memory, and Docker's
        # default 64 MB /dev/shm (deploy/compose.yaml sets no shm_size) cannot hold it.
        self.conn.execute(
            "SELECT set_config('maintenance_work_mem', %s, true),"
            " set_config('max_parallel_maintenance_workers', '0', true)",
            (self.maintenance_work_mem,),
        )
        for _name, definition in hnsw:
            self.conn.execute(definition)

    def set_identities(self) -> None:
        high = _sqlite_sequence(self.src)
        for table, column in _identities(self.conn):
            row = self.conn.execute(
                f"SELECT max({_ident(column)}) FROM {_ident(table)}"  # nosec B608 - catalog
            ).fetchone()
            source = SOURCE_TABLE.get(table, table)
            top = max(row[0] or 0, high.get(source, 0))
            seq = self.conn.execute(
                "SELECT pg_get_serial_sequence(%s, %s)", (table, column)
            ).fetchone()[0]
            if top > 0:
                self.conn.execute("SELECT setval(%s, %s, true)", (seq, top))
            else:
                self.conn.execute("SELECT setval(%s, 1, false)", (seq,))
            self.report.sequences[table] = top + 1

    # -- verification

    def count_labels(self) -> None:
        for table in VECTOR_TABLES:
            rows = self.src.execute(
                f'SELECT model, count(*) FROM "{table}" GROUP BY model'  # nosec B608
            ).fetchall()
            self.report.labels[table] = {r[0]: r[1] for r in rows}

    def verify(self) -> None:
        for plan in self.plans:
            count, top = _source_stats(self.src, plan)
            t = TableReport(plan.table, plan.source, count, top)
            t.target_count, t.target_max_id = _target_stats(self.conn, plan.table)
            if plan.table in VECTOR_TABLES:
                t.skipped = self._stale_count(plan.table)
                t.digest_ok = self._vector_digest_src(plan) == self._vector_digest_pg(plan)
            elif plan.table in TEXT_TABLES:
                t.digest_ok = self._text_digest_src(plan) == self._text_digest_pg(plan)
            self.report.tables.append(t)

    def _stale_count(self, table: str) -> int:
        if not self.drop_stale:
            return 0
        return self.src.execute(
            f'SELECT count(*) FROM "{table}" WHERE model IS NOT ?', (self.label,)  # nosec B608
        ).fetchone()[0]

    def _vector_digest_src(self, plan: _TablePlan) -> str:
        owner = VECTOR_TABLES[plan.table]
        h = hashlib.sha256()
        where = "WHERE model = ?" if self.drop_stale else ""
        params = (self.label,) if self.drop_stale else ()
        cur = self.src.execute(
            f'SELECT {owner}, chunk_no, text, vec FROM "{plan.table}" {where}'  # nosec B608
            f" ORDER BY {owner}, chunk_no",
            params,
        )
        for o, n, text, vec in cur:
            _feed(h, o, n, text, vec if isinstance(vec, bytes) else b"")
        return h.hexdigest()

    def _vector_digest_pg(self, plan: _TablePlan) -> str:
        owner = VECTOR_TABLES[plan.table]
        h = hashlib.sha256()
        with self.conn.cursor(binary=True) as cur:
            cur.execute(
                f"SELECT {owner}, chunk_no, text, vec FROM {_ident(plan.table)}"  # nosec B608
                " WHERE model_id = %s AND strategy = %s ORDER BY 1, 2",
                (self.model_id, self.key),
            )
            for o, n, text, vec in cur:
                _feed(h, o, n, text, _blob_from_pg(vec))
        return h.hexdigest()

    def _text_columns(self, plan: _TablePlan) -> list[str]:
        return [c.name for c in plan.columns if c.name not in (TEXT_TABLES[plan.table],)]

    def _text_digest_src(self, plan: _TablePlan) -> str:
        h = hashlib.sha256()
        cols = self._text_columns(plan)
        sel = ", ".join(_ident(c) for c in cols)
        cur = self.src.execute(
            f'SELECT rowid, {sel} FROM "{plan.source}" ORDER BY rowid'  # nosec B608
        )
        for row in cur:
            _feed(h, row[0], *("" if v is None else v for v in row[1:]))
        return h.hexdigest()

    def _text_digest_pg(self, plan: _TablePlan) -> str:
        h = hashlib.sha256()
        owner = TEXT_TABLES[plan.table]
        sel = ", ".join(_ident(c) for c in self._text_columns(plan))
        cur = self.conn.execute(
            f"SELECT {owner}, {sel} FROM {_ident(plan.table)} ORDER BY 1"  # nosec B608
        )
        for row in cur:
            _feed(h, *row)
        return h.hexdigest()


def _feed(h: Any, *values: Any) -> None:
    for v in values:
        if isinstance(v, (bytes, memoryview)):
            data = bytes(v)
        else:
            data = str(v).encode("utf-8", "surrogateescape")
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)


def _copy_type(col: _Column, vector_oid: int) -> Any:
    return {"bigint": "int8", "integer": "int4", "text": "text"}.get(col.type, vector_oid)


def _register_vector_dumper(conn: Any) -> int:
    """Register a binary dumper for pgvector by oid on ``conn``; return the oid."""
    from psycopg.adapt import Dumper
    from psycopg.pq import Format

    oid = conn.execute("SELECT 'vector'::regtype::oid").fetchone()[0]

    class VectorBinaryDumper(Dumper):
        format = Format.BINARY

        def dump(self, obj: bytes) -> bytes:
            return obj  # already pgvector's binary form (see _vector_value)

    VectorBinaryDumper.oid = oid
    conn.adapters.register_dumper(None, VectorBinaryDumper)
    return oid


def _hnsw_indexes(conn: Any) -> list[tuple[str, str]]:
    """``(name, CREATE INDEX statement)`` of the HNSW indexes on the vector tables."""
    rows = conn.execute(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema()"
        " AND tablename = ANY(%s) AND indexdef ILIKE '%%USING hnsw%%' ORDER BY indexname",
        (list(VECTOR_TABLES),),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _check_empty(conn: Any) -> None:
    full = [
        t
        for t in sorted(_target_tables(conn))
        if conn.execute(f"SELECT EXISTS (SELECT 1 FROM {_ident(t)})").fetchone()[0]  # nosec
    ]
    if full:
        raise TargetNotEmptyError(
            f"the target database already holds library rows (in {', '.join(full)}); "
            "the copy only fills an empty, freshly migrated database"
        )


def _resolve_space(embedder: VectorSpace) -> tuple[str, Strategy]:
    from culture_library.core.search import EmbeddingError

    try:
        return embedder.vector_space()
    except EmbeddingError:
        raise
    except Exception as exc:  # a custom embedder failing its lookup is the same refusal
        raise EmbeddingError(f"cannot resolve the vector space: {exc}") from exc


def copy_library(
    source: str | os.PathLike[str],
    conninfo: str,
    *,
    embedder: VectorSpace | None = None,
    drop_stale_vectors: bool = False,
    maintenance_work_mem: str = HNSW_BUILD_MEMORY,
    clock: Callable[[], float] = time.monotonic,
) -> Report:
    """Copy the SQLite library at ``source`` into the empty Postgres at ``conninfo``.

    Returns the report; ``report.status`` is ``copied`` (committed, every table matched),
    ``refused`` (problems; rolled back) or ``mismatch`` (verification failed; rolled back).
    Raises :class:`SourceError`, :class:`TargetNotEmptyError`, :class:`CopyError`, or
    :class:`~culture_library.core.search.EmbeddingError` when ``/capabilities`` cannot be read.
    """
    start = clock()
    embedder = embedder if embedder is not None else embedder_from_env()
    src = open_source(source)
    report = Report(source=str(source), mode="copy")
    try:
        model_id, strategy = _resolve_space(embedder)
        report.model_id, report.strategy = model_id, strategy.key()
        conn = open_target(conninfo)
        try:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (COPY_LOCK_KEY,))
            _check_empty(conn)
            run = _Run(
                src,
                conn,
                report,
                model_id,
                strategy,
                embedder.label,
                drop_stale_vectors,
                maintenance_work_mem,
            )
            run.count_labels()
            run.copy_all()
            if report.problems:
                report.status = "refused"
                conn.rollback()
                return report
            run.set_identities()
            run.verify()
            if report.ok:
                conn.commit()
                report.status = "copied"
            else:
                conn.rollback()
                report.status = "mismatch"
            return report
        except _psycopg().Error as exc:
            conn.rollback()
            first = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
            raise CopyError(f"Postgres refused the copy ({first}); nothing was written") from exc
        finally:
            conn.close()
    finally:
        src.close()
        report.elapsed_s = clock() - start


def verify_library(
    source: str | os.PathLike[str],
    conninfo: str,
    *,
    embedder: VectorSpace | None = None,
    drop_stale_vectors: bool = False,
    clock: Callable[[], float] = time.monotonic,
) -> Report:
    """Compare an already copied target with the SQLite source; writes nothing."""
    start = clock()
    embedder = embedder if embedder is not None else embedder_from_env()
    src = open_source(source)
    report = Report(source=str(source), mode="verify")
    try:
        model_id, strategy = _resolve_space(embedder)
        report.model_id, report.strategy = model_id, strategy.key()
        conn = open_target(conninfo)
        try:
            conn.read_only = True
            run = _Run(src, conn, report, model_id, strategy, embedder.label, drop_stale_vectors)
            run.count_labels()
            run.verify()
            report.status = "verified" if report.ok else "mismatch"
            return report
        finally:
            conn.rollback()
            conn.close()
    finally:
        src.close()
        report.elapsed_s = clock() - start
