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
    assert "--allow" in first and "--shushu" in first
    assert "--tunnel-name" in first
    assert "--apply" not in first, "first command must be the dry run"
    assert "--apply" in doc
    assert "127.0.0.1" not in re.search(r"--service \S+", first).group(0)


def test_doc_verification_steps():
    doc = DOC.read_text(encoding="utf-8")
    for needle in ("401", "302", "/api/health", "keys mint --name ops --scope admin", "grant run"):
        assert needle in doc, needle
