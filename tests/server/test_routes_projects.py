"""GET /api/projects and /api/domains: the project/domain index the site and MCP browse.

Deviation d18: the plan's site task (t20) needs project rows with domains, which no route
exposed. Only projects with at least one doc the caller can see are listed, so a private
repo's name and domains never reach a caller without private access.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402
from culture_library.server.auth import Principal, current_principal  # noqa: E402

A = "a" * 40
LIB = "https://github.com/agentculture/culture-library"
RULES = "https://github.com/agentculture/culture-rules"
NOVA = "https://github.com/agentculture/reachy_nova"


def _principal(*scopes: str, private: bool = False) -> Principal:
    return Principal(key_id="test", scopes=frozenset(scopes), private=private, kinds=None)


@pytest.fixture
def app(tmp_path):
    application = server_app.create_app()
    application.state.store = Store(tmp_path / "data")
    application.dependency_overrides[current_principal] = lambda: _principal(
        "read", "write", private=True
    )
    return application


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def _push(client, remote, path, domains, connected=()):
    r = client.post(
        "/api/repodocs",
        json={
            "remote": remote,
            "path": path,
            "commit": A,
            "committed_at": "2026-10-01T00:00:00Z",
            "default_branch_head": A,
            "content": f"# {path}\n",
            "project": {"domains": list(domains), "connected": list(connected)},
        },
    )
    assert r.status_code in (200, 201), r.text


@pytest.fixture
def seeded(client):
    _push(client, LIB, "docs/specs/a.md", ["library", "media"], [RULES])
    _push(client, LIB, "docs/plans/a.md", ["library", "media"], [RULES])
    _push(client, RULES, "docs/specs/r.md", ["mesh"])
    _push(client, NOVA, "docs/specs/n.md", ["media"])
    return client


def test_projects_lists_domains_connected_and_doc_counts(seeded):
    body = seeded.get("/api/projects").json()
    by_repo = {p["repo"]: p for p in body["projects"]}
    assert set(by_repo) == {
        "github.com/agentculture/culture-library",
        "github.com/agentculture/culture-rules",
        "github.com/agentculture/reachy_nova",
    }
    lib = by_repo["github.com/agentculture/culture-library"]
    assert lib["domains"] == ["library", "media"]
    assert lib["connected"] == ["github.com/agentculture/culture-rules"]
    assert lib["doc_count"] == 2
    assert by_repo["github.com/agentculture/reachy_nova"]["doc_count"] == 1


def test_domains_lists_each_domain_with_its_projects(seeded):
    body = seeded.get("/api/domains").json()
    by_domain = {d["domain"]: d["projects"] for d in body["domains"]}
    assert by_domain == {
        "library": ["github.com/agentculture/culture-library"],
        "media": ["github.com/agentculture/culture-library", "github.com/agentculture/reachy_nova"],
        "mesh": ["github.com/agentculture/culture-rules"],
    }


def test_caller_without_private_access_sees_no_projects_or_domains(seeded, app):
    app.dependency_overrides[current_principal] = lambda: _principal("read")
    assert seeded.get("/api/projects").json() == {"projects": []}
    assert seeded.get("/api/domains").json() == {"domains": []}


def test_project_with_only_removed_docs_is_hidden(seeded):
    r = seeded.post(
        "/api/repodocs",
        json={"remote": NOVA, "path": "docs/specs/n.md", "commit": A, "tombstone": True},
    )
    assert r.status_code == 200, r.text
    repos = {p["repo"] for p in seeded.get("/api/projects").json()["projects"]}
    assert "github.com/agentculture/reachy_nova" not in repos
    media = {d["domain"]: d["projects"] for d in seeded.get("/api/domains").json()["domains"]}
    assert media["media"] == ["github.com/agentculture/culture-library"]


def test_read_scope_required(seeded, app):
    app.dependency_overrides[current_principal] = lambda: _principal(private=True)
    assert seeded.get("/api/projects").status_code == 403
