"""Tests for the Postgres test infrastructure in tests/conftest.py (independent of the store)."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
import yaml

from tests import conftest as infra

ROOT = Path(__file__).resolve().parent.parent


def _pytest(env_extra: dict, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CULTURE_LIBRARY_")}
    env.update(env_extra)
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args]
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


def test_worker_database_is_isolated_and_store_dsn_points_at_it(pg_dsn, pg_admin_dsn):
    assert os.environ[infra.STORE_DSN_ENV] == pg_dsn
    assert pg_dsn != pg_admin_dsn
    with psycopg.connect(pg_dsn) as conn:
        name = conn.execute("SELECT current_database()").fetchone()[0]
    assert name.startswith(infra.DB_PREFIX)
    assert os.environ.get("PYTEST_XDIST_WORKER", "main") in name


def test_clone_inherits_migrated_template_and_reset_restores_seed(pg_admin_dsn):
    uid = uuid.uuid4().hex[:8]
    tpl, clone = f"t2_tpl_{uid}", f"t2_clone_{uid}"

    def fake_migrate(conn):
        conn.execute("CREATE TABLE meta (k text PRIMARY KEY, v text)")
        conn.execute("INSERT INTO meta VALUES ('schema', '1')")
        conn.execute("CREATE TABLE things (id bigserial PRIMARY KEY, name text)")
        return ["0001"]

    try:
        infra.build_template(pg_admin_dsn, tpl, fake_migrate)
        dsn = infra.clone_database(pg_admin_dsn, tpl, clone)
        with psycopg.connect(dsn, autocommit=True) as conn:
            assert conn.execute("SELECT v FROM meta").fetchone()[0] == "1"
            seeded = infra.snapshot_seed(conn)
            assert seeded == ["meta"]
            conn.execute("INSERT INTO things (name) VALUES ('a'), ('b')")
            conn.execute("UPDATE meta SET v = '2'")
            infra.reset_database(conn, seeded)
            assert conn.execute("SELECT count(*) FROM things").fetchone()[0] == 0
            assert conn.execute("SELECT v FROM meta").fetchone()[0] == "1"
            # identities restart, so ids are reproducible from test to test
            conn.execute("INSERT INTO things (name) VALUES ('c')")
            assert conn.execute("SELECT id FROM things").fetchone()[0] == 1
    finally:
        infra.drop_database(pg_admin_dsn, clone)
        infra.drop_database(pg_admin_dsn, tpl)


def test_each_test_starts_empty_first(pg_dsn):
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS t2_probe (n int)")
        conn.execute("INSERT INTO t2_probe VALUES (1)")


def test_each_test_starts_empty_second(pg_dsn):
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS t2_probe (n int)")
        assert conn.execute("SELECT count(*) FROM t2_probe").fetchone()[0] == 0


@pytest.mark.parametrize("dsn_env", [{}, {infra.TEST_DSN_ENV: "postgresql://x@127.0.0.1:1/x"}])
def test_unreachable_or_unset_postgres_fails_once_naming_the_setting(dsn_env):
    result = _pytest(dsn_env, "tests/test_zero_deps.py", "-q")
    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert infra.TEST_DSN_ENV in out
    assert out.count("no reachable Postgres") == 1
    assert "skipped" not in out and "passed" not in out


def test_run_drops_every_database_it_created(pg_admin_dsn):
    uid = "t2drop" + uuid.uuid4().hex[:6]
    env = {infra.TEST_DSN_ENV: pg_admin_dsn, infra.RUN_UID_ENV: uid}
    result = _pytest(env, "tests/test_zero_deps.py", "-q", "-n", "2")
    assert result.returncode == 0, result.stdout + result.stderr
    with psycopg.connect(pg_admin_dsn, autocommit=True) as conn:
        left = conn.execute("SELECT datname FROM pg_database WHERE datname LIKE %s", (f"%{uid}%",))
        assert left.fetchall() == []


def _job(path: str, job: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text())["jobs"][job]


@pytest.mark.parametrize("path", [".github/workflows/tests.yml", ".github/workflows/publish.yml"])
def test_ci_test_job_runs_pgvector_service_and_passes_dsn(path):
    job = _job(path, "test")
    pg = next(s for s in job["services"].values() if s["image"] == "pgvector/pgvector:pg17")
    assert any("5432" in p for p in pg["ports"])
    assert "--locale=C" in pg["env"]["POSTGRES_INITDB_ARGS"]
    dsns = [
        str(v)
        for step in job["steps"]
        for v in step.get("env", {}).items()
        if v[0] == infra.TEST_DSN_ENV
    ]
    dsns += [str(job.get("env", {}).get(infra.TEST_DSN_ENV, ""))]
    assert any("127.0.0.1" in d or "localhost" in d for d in dsns)
