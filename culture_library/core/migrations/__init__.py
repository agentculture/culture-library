"""The versioned schema migration runner: numbered SQL files applied once each.

Every schema change ships as ``core/migrations/NNNN_name.sql`` (four digits, numbered from
0000 with no gaps). :func:`migrate` applies the files a database has not seen yet, in order,
inside the caller's transaction, and records each in ``schema_version`` (created by 0000).
A database that is already current costs two reads and runs no DDL. Postgres DDL is
transactional, so a failing file rolls back every file applied in the same call.

Two processes opening a fresh database at once are serialised by a transaction-level
advisory lock; the loser re-reads ``schema_version`` after the winner commits and applies
nothing. The module never imports psycopg: it only calls ``conn.execute``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

from ..errors import CoreError

# pg_advisory_xact_lock key for the runner ("culture-library migrations"); any fixed bigint.
LOCK_KEY = 0x6C6962_6D696772

_FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class MigrationError(CoreError):
    """The migrations on disk or the database's ``schema_version`` cannot be reconciled."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str  # the file name without ".sql", e.g. "0000_schema_version"
    sql: str


def load(directory: str | Path | None = None) -> list[Migration]:
    """The migrations in ``directory`` (default: the shipped package files), in order."""
    root: Any = Path(directory) if directory is not None else resources.files(__name__)
    found: dict[int, Migration] = {}
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        if not entry.name.endswith(".sql"):
            continue
        match = _FILE_RE.match(entry.name)
        if match is None:
            raise MigrationError(
                f"migration file {entry.name!r} is not named NNNN_name.sql "
                "(four digits, then lowercase letters, digits and underscores)"
            )
        version = int(match.group(1))
        if version in found:
            raise MigrationError(
                f"duplicate migration version {version:04d}: {found[version].name} and "
                f"{entry.name[:-4]}; renumber one of them"
            )
        found[version] = Migration(version, entry.name[:-4], entry.read_text(encoding="utf-8"))
    versions = sorted(found)
    if not versions or versions[0] != 0:
        raise MigrationError("migrations must start at 0000 (the schema_version bootstrap)")
    for expected, version in enumerate(versions):
        if version != expected:
            raise MigrationError(
                f"migration numbering has a gap: {expected:04d} is missing before "
                f"{found[version].name}"
            )
    return [found[v] for v in versions]


_SHIPPED = load()
CURRENT_VERSION = _SHIPPED[-1].version


# A plain MVCC read of pg_class, deliberately not to_regclass(): to_regclass caches a negative
# syscache entry when the table is missing, and taking the advisory lock does not invalidate
# it, so an opener that lost the race would still see "no schema_version" after the winner
# committed and re-run 0000 (DuplicateTable). Measured with test_concurrent_first_opens.
_EXISTS_SQL = (
    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class"
    " WHERE relname = 'schema_version' AND relnamespace = current_schema()::regnamespace)"
)


def _applied(conn: Any) -> dict[int, str]:
    exists = conn.execute(_EXISTS_SQL).fetchone()[0]
    if not exists:
        return {}
    rows = conn.execute("SELECT version, name FROM schema_version").fetchall()
    return {row[0]: row[1] for row in rows}


def _pending(applied: dict[int, str], shipped: Iterable[Migration]) -> list[Migration]:
    """The migrations still to apply; raises if the database's history disagrees with ours."""
    shipped = list(shipped)
    known = {m.version: m.name for m in shipped}
    current = shipped[-1].version
    ahead = sorted(v for v in applied if v > current)
    if ahead:
        raise MigrationError(
            f"the database's schema_version is {ahead[-1]}, ahead of this culture-library "
            f"(it ships migrations up to {current}): a newer culture-library migrated it. "
            "Run that release or a later one, or restore a backup taken before the upgrade."
        )
    for version, name in sorted(applied.items()):
        if known[version] != name:
            raise MigrationError(
                f"schema_version records migration {version} as {name!r}, but this release "
                f"ships {known[version]!r}: the database and the code have diverged. Run the "
                "release that migrated it, or restore a backup that matches this release."
            )
    pending = [m for m in shipped if m.version not in applied]
    if pending and applied and pending[0].version < max(applied):
        raise MigrationError(
            f"schema_version is missing migration {pending[0].name} below applied version "
            f"{max(applied)}: the history has a hole. Restore a backup that matches a release."
        )
    return pending


def migrate(conn: Any, shipped: Iterable[Migration] | None = None) -> list[str]:
    """Apply the pending migrations inside ``conn``'s open transaction; return their names.

    ``conn`` must not be in autocommit mode: the advisory lock and the DDL have to share the
    caller's transaction so that they commit (or roll back) together. The caller commits.
    """
    if getattr(conn, "autocommit", False):
        raise MigrationError(
            "migrate() needs a connection inside a transaction (autocommit off), so the "
            "migrations and their schema_version rows commit or roll back together"
        )
    shipped = list(shipped) if shipped is not None else _SHIPPED
    pending = _pending(_applied(conn), shipped)
    if not pending:
        return []
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
    # Re-read under the lock: a racing opener may have committed the same migrations.
    pending = _pending(_applied(conn), shipped)
    for migration in pending:
        conn.execute(migration.sql)
        conn.execute(
            "INSERT INTO schema_version (version, name, applied_at) VALUES (%s, %s, %s)",
            (
                migration.version,
                migration.name,
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
    return [m.name for m in pending]
