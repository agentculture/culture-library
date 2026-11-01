"""Fixtures shared by the backup tests: pg client tools and an empty scratch database.

The client tools come from PATH (CI installs postgresql-client-17). On a host without them the
``pg_bin`` fixture builds wrappers that run the tools inside the ``culture-library-pgtest``
container (docs/operations/testing.md); with neither, the tests fail, never skip.
"""

import os
import secrets
import shutil
import stat
import subprocess
import uuid

import pytest

from culture_library.core import backup

TEST_CONTAINER = "culture-library-pgtest"
_WRAPPER = """#!/usr/bin/env python3
# test-only: run the real {tool} inside the throwaway Postgres container.
import subprocess, sys
args = [a.replace("55432", "5432") for a in sys.argv[1:]]
sys.exit(subprocess.call(["docker", "exec", "-i", "{container}", "{tool}", *args]))
"""


@pytest.fixture(scope="session", autouse=True)
def pg_bin(tmp_path_factory):
    """Make pg_dump/pg_restore reachable: PATH if present, else docker-exec wrappers."""
    if shutil.which("pg_dump") and shutil.which("pg_restore"):
        yield None
        return
    bin_dir = tmp_path_factory.mktemp("pgbin")
    for tool in ("pg_dump", "pg_restore"):
        path = bin_dir / tool
        path.write_text(_WRAPPER.format(tool=tool, container=TEST_CONTAINER))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    os.environ[backup.PG_BIN_DIR_ENV] = str(bin_dir)
    probe = subprocess.run([str(bin_dir / "pg_dump"), "--version"], capture_output=True)
    assert probe.returncode == 0, (
        "no pg_dump on PATH and no running container "
        f"{TEST_CONTAINER}: {probe.stderr.decode()[:200]}"
    )
    yield bin_dir
    os.environ.pop(backup.PG_BIN_DIR_ENV, None)


def _empty_database(admin_dsn: str, name: str, *, owner: str | None = None, vector=True) -> None:
    """Create an empty database the way live's init script does: the superuser makes it and
    its ``vector`` extension, since the application role may do neither."""
    import psycopg
    from psycopg.conninfo import make_conninfo

    from tests.conftest import _ident

    owned = f" OWNER {_ident(owner)}" if owner else ""
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {_ident(name)}{owned}")
    if vector:
        with psycopg.connect(make_conninfo(admin_dsn, dbname=name), autocommit=True) as conn:
            conn.execute("CREATE EXTENSION vector")


@pytest.fixture
def scratch_dsn(pg_admin_dsn):
    """An empty database (no tables, the vector extension), dropped afterwards."""
    from psycopg.conninfo import make_conninfo

    from tests.conftest import drop_database

    name = f"culture_library_test_bk_{uuid.uuid4().hex[:10]}"
    _empty_database(pg_admin_dsn, name)
    try:
        yield make_conninfo(pg_admin_dsn, dbname=name)
    finally:
        drop_database(pg_admin_dsn, name)


@pytest.fixture
def no_vector_dsn(pg_admin_dsn):
    """An empty database without the vector extension, dropped afterwards."""
    from psycopg.conninfo import make_conninfo

    from tests.conftest import drop_database

    name = f"culture_library_test_bk_{uuid.uuid4().hex[:10]}"
    _empty_database(pg_admin_dsn, name, vector=False)
    try:
        yield make_conninfo(pg_admin_dsn, dbname=name)
    finally:
        drop_database(pg_admin_dsn, name)


@pytest.fixture
def app_role_dsn(pg_admin_dsn):
    """An empty database owned by a role shaped like live's ``culture_library`` (not a
    superuser, cannot create roles or databases), connected as that role."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from tests.conftest import _ident, drop_database

    suffix = uuid.uuid4().hex[:10]
    role, name = f"culture_library_test_role_{suffix}", f"culture_library_test_bk_{suffix}"
    password = secrets.token_hex(8)
    create = sql.SQL(
        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        " PASSWORD {}"  # a utility statement takes no bind parameters, so compose a literal
    ).format(sql.Identifier(role), sql.Literal(password))
    with psycopg.connect(pg_admin_dsn, autocommit=True) as admin:
        admin.execute(create)
    try:
        _empty_database(pg_admin_dsn, name, owner=role)
        yield make_conninfo(pg_admin_dsn, dbname=name, user=role, password=password)
    finally:
        drop_database(pg_admin_dsn, name)
        with psycopg.connect(pg_admin_dsn, autocommit=True) as admin:
            admin.execute(f"DROP ROLE IF EXISTS {_ident(role)}")
