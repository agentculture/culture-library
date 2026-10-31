"""Static checks on the spark deployment files (nothing is built or started)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "deploy" / "compose.yaml"
DOC = ROOT / "docs" / "operations" / "deploy.md"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_files_exist():
    for name in ("Dockerfile", "compose.yaml", ".env.example"):
        assert (ROOT / "deploy" / name).is_file(), name
    assert DOC.is_file()


def test_services(compose):
    assert {"library", "mcp", "cloudflared"} <= set(compose["services"])


def test_named_data_volume(compose):
    vols = compose["services"]["library"]["volumes"]
    assert any(str(v).endswith(":/data") for v in vols)
    mount = next(v for v in vols if str(v).endswith(":/data"))
    assert "culture-library-data" in mount
    assert "culture-library-data" in compose["volumes"]


@pytest.mark.parametrize("svc", ["library", "mcp", "cloudflared"])
def test_restart_and_healthcheck(compose, svc):
    s = compose["services"][svc]
    assert s["restart"] == "unless-stopped"
    hc = s["healthcheck"]
    interval = int(str(hc["interval"]).rstrip("s"))
    retries = int(hc["retries"])
    start = int(str(hc.get("start_period", "0s")).rstrip("s"))
    assert start + interval * retries <= 120


@pytest.mark.parametrize("svc", ["library", "mcp"])
def test_ports_bound_to_loopback_default(compose, svc):
    ports = compose["services"][svc]["ports"]
    assert ports
    for p in ports:
        assert str(p).startswith("${LIBRARY_PUBLISH_BIND:-127.0.0.1}:"), p


def test_mcp_never_public(compose):
    # cloudflared is the only public path; it has no ports and MCP is not its target.
    cf = compose["services"]["cloudflared"]
    assert "ports" not in cf
    text = COMPOSE.read_text(encoding="utf-8")
    assert "8797" in text
    doc = DOC.read_text(encoding="utf-8")
    assert not re.search(r"--service\s+http://mcp", doc)
    assert "--service http://library:8796" in doc


def test_cloudflared_only_in_remote_profile(compose):
    assert compose["services"]["cloudflared"]["profiles"] == ["remote"]
    for name, svc in compose["services"].items():
        if name != "cloudflared":
            assert "profiles" not in svc, name
    assert "cloudflared" not in compose["services"]["mcp"].get("depends_on", {})


def test_mcp_reaches_library_by_service_name(compose):
    env = compose["services"]["mcp"]["environment"]
    assert env["CULTURE_LIBRARY_URL"] == "http://library:8796"


def test_no_literal_secrets():
    for path in (COMPOSE, ROOT / "deploy" / ".env.example", ROOT / "deploy" / "Dockerfile"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"clk_[A-Za-z0-9]{8,}", text), path
        assert not re.search(r"eyJ[A-Za-z0-9_-]{20,}", text), path
        for line in text.splitlines():
            m = re.match(r"\s*(?:export\s+)?\w*(TOKEN|KEY|SECRET)\w*\s*[:=]\s*(\S+)", line)
            if m and not line.lstrip().startswith("#"):
                assert m.group(2) in ('""', "''") or m.group(2).startswith("${"), line


def test_doc_has_cultureflare_dry_run():
    doc = DOC.read_text(encoding="utf-8")
    cmd = re.search(r"^cultureflare remote-login setup .*?(?=^```)", doc, re.M | re.S)
    assert cmd, "no cultureflare setup command"
    first = cmd.group(0)
    assert "--hostname library.culture.dev" in first
    assert "--service http://library:8796" in first
    assert "--allow" in first
    assert "--shushu" in first
    assert "--tunnel-name" in first
    assert "--apply" not in first, "first command must be the dry run"
    assert "--apply" in doc
    assert "127.0.0.1" not in re.search(r"--service \S+", first).group(0)


def test_doc_verification_steps():
    doc = DOC.read_text(encoding="utf-8")
    for needle in ("401", "302", "/api/health", "keys mint --name ops --scope admin", "grant run"):
        assert needle in doc, needle


INIT_SQL = ROOT / "deploy" / "postgres-init" / "01-library.sql"


def test_postgres_service(compose):
    pg = compose["services"]["postgres"]
    assert pg["image"] == "pgvector/pgvector:pg17"
    assert "ports" not in pg
    assert pg["restart"] == "unless-stopped"
    assert any("culture-library-pgdata" in str(v) for v in pg["volumes"])
    assert "culture-library-pgdata" in compose["volumes"]
    assert "pg_isready" in " ".join(pg["healthcheck"]["test"])
    assert "127.0.0.1" in " ".join(pg["healthcheck"]["test"])  # not the init-time socket


def test_postgres_has_room_for_parallel_index_builds(compose):
    # Docker's default /dev/shm is 64 MB; a parallel HNSW build over the live vectors needs
    # about 1 GB of shared memory and fails with DiskFull without it (plan risk r7).
    shm = str(compose["services"]["postgres"].get("shm_size", ""))
    assert shm.lower() in {"1g", "1gb", "2g", "2gb"}


def test_library_waits_for_healthy_postgres(compose):
    dep = compose["services"]["library"]["depends_on"]["postgres"]
    assert dep["condition"] == "service_healthy"
    env = compose["services"]["library"]["environment"]
    assert env["CULTURE_LIBRARY_PG_HOST"] == "postgres"
    assert env["CULTURE_LIBRARY_PG_DATABASE"] == "culture_library"
    assert env["CULTURE_LIBRARY_PG_USER"] == "culture_library"
    assert "POSTGRES_PASSWORD" not in env  # the library never gets the superuser password


def test_postgres_init_script_shape():
    sql = INIT_SQL.read_text(encoding="utf-8")
    assert "NOSUPERUSER" in sql and "NOCREATEROLE" in sql and "NOCREATEDB" in sql
    assert "LC_COLLATE 'C'" in sql and "OWNER culture_library" in sql
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    assert ":'apppw'" in sql and "\\getenv apppw CULTURE_LIBRARY_PG_PASSWORD" in sql
