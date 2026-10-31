"""Fixtures shared by the backup tests: pg client tools and an empty scratch database.

The client tools come from PATH (CI installs postgresql-client-17). On a host without them the
``pg_bin`` fixture builds wrappers that run the tools inside the ``culture-library-pgtest``
container (docs/operations/testing.md); with neither, the tests fail, never skip.
"""

import os
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


@pytest.fixture
def scratch_dsn(pg_admin_dsn):
    """An empty database (no tables), dropped afterwards."""
    from psycopg.conninfo import make_conninfo

    from tests.conftest import _ident, drop_database

    name = f"culture_library_test_bk_{uuid.uuid4().hex[:10]}"
    import psycopg

    with psycopg.connect(pg_admin_dsn, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {_ident(name)}")
    try:
        yield make_conninfo(pg_admin_dsn, dbname=name)
    finally:
        drop_database(pg_admin_dsn, name)
