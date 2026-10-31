"""Postgres test infrastructure shared by the whole suite.

The suite needs a Postgres with pgvector, named by ``CULTURE_LIBRARY_TEST_PG_DSN`` (see
``docs/operations/testing.md`` for the one-line ``docker run``). Isolation, from coarse to fine:

* the controller process builds one migrated *template* database per run (the DSN's role needs
  CREATEDB);
* each xdist worker clones the template into its own database (``CREATE DATABASE ... TEMPLATE``)
  and points ``CULTURE_LIBRARY_PG_DSN`` (what the store reads) at it;
* each test starts from empty tables: ``TRUNCATE ... RESTART IDENTITY CASCADE``, with rows the
  migrations seeded restored from a snapshot. Measured on a 40-table schema, truncating costs
  about 15 ms against about 105 ms for a per-test clone, so a clone per test is not worth it.

With no reachable Postgres the run stops before collection with one error naming the setting;
tests are never skipped for lack of a database.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import pytest

TEST_DSN_ENV = "CULTURE_LIBRARY_TEST_PG_DSN"
STORE_DSN_ENV = "CULTURE_LIBRARY_PG_DSN"
RUN_UID_ENV = "CULTURE_LIBRARY_TEST_RUN_UID"
SEED_SCHEMA = "_test_seed"
DB_PREFIX = "culture_library_test_"

_NO_PG = (
    f"no reachable Postgres for the test suite: set {TEST_DSN_ENV} to a DSN of a Postgres 17 with "
    "pgvector whose role may CREATE DATABASE, e.g. postgresql://postgres@127.0.0.1:55432/postgres "
    "(one-line docker run: docs/operations/testing.md)."
)


def _psycopg():
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - psycopg is in the dev group
        raise pytest.UsageError(f"{_NO_PG} The psycopg driver is not installed: {exc}") from exc
    return psycopg


def _with_dbname(dsn: str, dbname: str) -> str:
    from psycopg.conninfo import make_conninfo

    return make_conninfo(dsn, dbname=dbname)


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def check_admin(dsn: str | None) -> str:
    """Return a usable admin DSN or raise the one clear UsageError."""
    if not dsn:
        raise pytest.UsageError(_NO_PG)
    psycopg = _psycopg()
    try:
        with psycopg.connect(dsn, connect_timeout=5, autocommit=True) as conn:
            conn.execute("SELECT 1")
    except psycopg.Error as exc:
        raise pytest.UsageError(f"{_NO_PG} ({TEST_DSN_ENV} could not connect: {exc})") from exc
    return dsn


def default_migrate(conn) -> list[str]:
    """Apply the repo's migrations when they exist; otherwise leave the template empty."""
    try:
        from culture_library.core.migrations import migrate
    except ImportError:
        return []
    return migrate(conn)


def build_template(admin_dsn: str, name: str, migrate: Callable | None = None) -> str:
    """Create database *name* and migrate it; return its DSN."""
    psycopg = _psycopg()
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {_ident(name)}")
    dsn = _with_dbname(admin_dsn, name)
    with psycopg.connect(dsn) as conn:  # one transaction: migrate() refuses autocommit
        (migrate or default_migrate)(conn)
        conn.commit()
    return dsn


def clone_database(admin_dsn: str, template: str, name: str) -> str:
    psycopg = _psycopg()
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {_ident(name)} TEMPLATE {_ident(template)}")
    return _with_dbname(admin_dsn, name)


def drop_database(admin_dsn: str, name: str) -> None:
    psycopg = _psycopg()
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {_ident(name)} WITH (FORCE)")


def _tables(conn) -> list[str]:
    rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
    return [r[0] for r in rows]


def snapshot_seed(conn) -> list[str]:
    """Copy every table that has rows right after migration into a side schema.

    Migration bookkeeping and any seed rows must survive the per-test truncate.
    """
    seeded = []
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SEED_SCHEMA}")
    for table in _tables(conn):
        if conn.execute(f"SELECT EXISTS (SELECT 1 FROM {_ident(table)})").fetchone()[0]:
            conn.execute(f"CREATE TABLE {SEED_SCHEMA}.{_ident(table)} AS TABLE {_ident(table)}")
            seeded.append(table)
    return seeded


def _indexes(conn) -> set[str]:
    rows = conn.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'").fetchall()
    return {r[0] for r in rows}


def reset_database(conn, seeded: list[str], indexes: set[str] | None = None) -> None:
    """Empty every public table and restart identities, then restore the seeded rows.

    With ``indexes`` (the public indexes right after migration), also drop any index a test
    created since: the indexer builds an HNSW index for each new (model, strategy) pair, and a
    truncate alone would leak it into later tests on the same worker.
    """
    tables = _tables(conn)
    if not tables:
        return
    conn.execute("SET lock_timeout = '10s'")
    for name in sorted(_indexes(conn) - indexes) if indexes is not None else ():
        conn.execute(f"DROP INDEX IF EXISTS {_ident(name)}")
    names = ", ".join(_ident(t) for t in tables)
    conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    for table in seeded:
        conn.execute(f"INSERT INTO {_ident(table)} SELECT * FROM {SEED_SCHEMA}.{_ident(table)}")


def _run_uid() -> str:
    return os.environ.setdefault(RUN_UID_ENV, uuid.uuid4().hex[:12])


def _is_worker(config) -> bool:
    return hasattr(config, "workerinput")


def pytest_configure(config):
    _run_uid()  # fixed before xdist spawns workers, which inherit the environment


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session):
    """Controller only: fail early and clearly, then build the shared migrated template."""
    config = session.config
    if _is_worker(config):
        return
    admin = check_admin(os.environ.get(TEST_DSN_ENV))
    name = f"{DB_PREFIX}tpl_{_run_uid()}"
    config._pg_template = name
    config._pg_admin = admin
    build_template(admin, name)


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    name = getattr(config, "_pg_template", None)
    if name and not _is_worker(config):
        drop_database(config._pg_admin, name)


@pytest.fixture(scope="session")
def pg_admin_dsn() -> str:
    return check_admin(os.environ.get(TEST_DSN_ENV))


@pytest.fixture(scope="session")
def pg_dsn(pg_admin_dsn) -> Iterator[str]:
    """This worker's own database, cloned from the migrated template and dropped afterwards."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    uid = _run_uid()
    dsn = clone_database(pg_admin_dsn, f"{DB_PREFIX}tpl_{uid}", f"{DB_PREFIX}{uid}_{worker}")
    prior = os.environ.get(STORE_DSN_ENV)
    os.environ[STORE_DSN_ENV] = dsn
    try:
        yield dsn
    finally:
        if prior is None:
            os.environ.pop(STORE_DSN_ENV, None)
        else:
            os.environ[STORE_DSN_ENV] = prior
        drop_database(pg_admin_dsn, f"{DB_PREFIX}{uid}_{worker}")


@pytest.fixture(scope="session")
def _pg_control(pg_dsn) -> Iterator[tuple]:
    psycopg = _psycopg()
    conn = psycopg.connect(pg_dsn, autocommit=True)
    try:
        yield conn, snapshot_seed(conn), _indexes(conn)
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _pg_isolation(_pg_control, pg_dsn) -> None:
    """Every test starts on empty tables in this worker's database."""
    conn, seeded, indexes = _pg_control
    reset_database(conn, seeded, indexes)
