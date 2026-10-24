"""POST/GET /api/repodocs (plan task t8).

The live sample reads committed docs from five PUBLIC agentculture repos cloned under
~/git (visibility verified with ``gh repo view agentculture/<r> --json visibility`` on
2026-10-24) and skips cleanly when they are absent (CI). The test itself uses read-only
``git ls-tree`` / ``git show`` / ``git log`` to obtain committed content; the library code
under test never invokes git.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 - read-only git plumbing in tests only
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402
from culture_library.server.auth import Principal, current_principal  # noqa: E402

GIT_ROOT = Path.home() / "git"
PUBLIC_REPOS = ("devague", "culture-rules", "embodiment", "lobes-cli", "pocketsync")
LOBES_CLONES = (
    "lobes-cli",
    "lobes-cli-orin-deciders",
    "lobes-cli-orin-nano",
    "lobes-cli-stt-m4a",
    "model-gear",
)
DOC_PREFIXES = ("docs/specs/", "docs/plans/", "docs/deliveries/", ".devague/")
FIXTURES = Path(__file__).resolve().parents[1] / "core" / "fixtures_repodocs"
A = "a" * 40
B = "b" * 40


def _principal(*scopes: str, private: bool = False, kinds=None) -> Principal:
    return Principal(
        key_id="test",
        scopes=frozenset(scopes),
        private=private,
        kinds=frozenset(kinds) if kinds else None,
    )


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


def _body(**kw) -> dict:
    body = dict(
        remote="https://github.com/agentculture/devague",
        path="docs/specs/x.md",
        commit=A,
        committed_at="2026-10-01T00:00:00Z",
        default_branch_head=A,
        visibility="public",
        content="hello\n",
    )
    body.update(kw)
    return body


def test_post_creates_then_dedups(client: TestClient) -> None:
    r = client.post("/api/repodocs", json=_body())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["created"]
    assert data["current_changed"]
    assert data["doc"]["repo"] == "github.com/agentculture/devague"
    assert data["doc"]["kind"] == "spec"
    again = client.post(
        "/api/repodocs", json=_body(remote="git@github.com:agentculture/devague.git")
    )
    assert again.status_code == 200
    assert again.json()["doc"]["id"] == data["doc"]["id"]
    assert not again.json()["created"]


def test_get_doc_versions_and_content(client: TestClient) -> None:
    doc_id = client.post("/api/repodocs", json=_body()).json()["doc"]["id"]
    client.post(
        "/api/repodocs",
        json=_body(
            commit=B, default_branch_head=A, content="feat\n", committed_at="2026-10-05T00:00:00Z"
        ),
    )
    r = client.get(f"/api/repodocs/{doc_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["doc"]["current_commit"] == A
    assert {v["commit"] for v in data["versions"]} == {A, B}
    assert client.get(f"/api/repodocs/{doc_id}/content").text == "hello\n"
    assert client.get(f"/api/repodocs/{doc_id}/content", params={"commit": B}).text == "feat\n"
    assert (
        client.get(f"/api/repodocs/{doc_id}/content", params={"commit": "e" * 40}).status_code
        == 404
    )


def test_list_and_lookup(client: TestClient) -> None:
    client.post("/api/repodocs", json=_body())
    client.post("/api/repodocs", json=_body(remote="https://github.com/agentculture/pocketsync"))
    assert len(client.get("/api/repodocs").json()["docs"]) == 2
    one = client.get("/api/repodocs", params={"remote": "agentculture/devague"})
    assert one.status_code == 400  # not a remote form
    one = client.get(
        "/api/repodocs", params={"remote": "https://github.com/agentculture/devague.git"}
    )
    assert [d["repo"] for d in one.json()["docs"]] == ["github.com/agentculture/devague"]
    hit = client.get(
        "/api/repodocs",
        params={"remote": "git@github.com:agentculture/devague", "path": "docs/specs/x.md"},
    )
    assert len(hit.json()["docs"]) == 1


def test_validation_errors_are_400(client: TestClient) -> None:
    assert client.post("/api/repodocs", json=_body(commit="nope")).status_code == 400
    assert client.post("/api/repodocs", json=_body(remote="/abs/only")).status_code == 400
    missing = _body()
    del missing["content"]
    assert client.post("/api/repodocs", json=missing).status_code == 422


def test_private_docs_hidden_from_public_principal(app, client: TestClient) -> None:
    doc_id = client.post("/api/repodocs", json=_body(visibility=None)).json()["doc"]["id"]
    app.dependency_overrides[current_principal] = lambda: _principal("read")
    assert client.get(f"/api/repodocs/{doc_id}").status_code == 404
    assert client.get(f"/api/repodocs/{doc_id}/content").status_code == 404
    assert client.get("/api/repodocs").json()["docs"] == []


def test_scopes_enforced(app, client: TestClient) -> None:
    app.dependency_overrides[current_principal] = lambda: _principal("read")
    assert client.post("/api/repodocs", json=_body()).status_code == 403
    app.dependency_overrides[current_principal] = lambda: _principal("write", kinds={"plan"})
    assert client.post("/api/repodocs", json=_body()).status_code == 403  # kind spec
    assert client.post("/api/repodocs", json=_body(path="docs/plans/p.md")).status_code == 201
    assert client.get("/api/repodocs").status_code == 403


def test_unauthenticated_is_refused(tmp_path) -> None:
    application = server_app.create_app()
    application.state.store = Store(tmp_path / "data")
    assert TestClient(application).post("/api/repodocs", json=_body()).status_code == 401


def test_fixture_sample_through_api(client: TestClient) -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    for e in manifest:
        content = (FIXTURES / e["file"]).read_text()
        r = client.post(
            "/api/repodocs",
            json=_body(
                remote=e["remote"],
                path=e["path"],
                commit=e["commit"],
                committed_at=e["committed_at"],
                default_branch_head=e["commit"],
                visibility=e["visibility"],
                content=content,
            ),
        )
        assert r.status_code == 201, r.text
        assert client.get(f"/api/repodocs/{r.json()['doc']['id']}/content").text == content


# --- live sample from real public clones ----------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # nosec B603 B607 - read-only plumbing, fixed argv
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def _live_repos() -> list[Path]:
    repos = [GIT_ROOT / r for r in PUBLIC_REPOS]
    if not all((r / ".git").exists() for r in repos):
        pytest.skip("public sample clones not present under ~/git")
    return repos


def test_live_public_repo_sample_round_trip(client: TestClient) -> None:
    pushed: dict[int, str] = {}
    repos_seen = set()
    for repo in _live_repos():
        head = _git(repo, "rev-parse", "HEAD").strip()
        committed_at = _git(repo, "log", "-1", "--format=%cI", "HEAD").strip()
        remote = _git(repo, "remote", "get-url", "origin").strip()
        paths = [
            p
            for p in _git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
            if p.startswith(DOC_PREFIXES) and p.endswith((".md", ".json"))
        ][:4]
        assert paths, f"no docs found in {repo}"
        for path in paths:
            content = _git(repo, "show", f"HEAD:{path}")
            r = client.post(
                "/api/repodocs",
                json=dict(
                    remote=remote,
                    path=path,
                    commit=head,
                    committed_at=committed_at,
                    default_branch_head=head,
                    visibility="public",
                    content=content,
                ),
            )
            assert r.status_code in (200, 201), r.text
            doc = r.json()["doc"]
            repos_seen.add(doc["repo"])
            pushed[doc["id"]] = content
    assert len(repos_seen) >= 5
    for doc_id, content in pushed.items():
        assert client.get(f"/api/repodocs/{doc_id}/content").text == content
    assert len(client.get("/api/repodocs").json()["docs"]) == len(pushed)


def test_live_lobes_clones_collapse_to_one_repo(client: TestClient) -> None:
    clones = [GIT_ROOT / c for c in LOBES_CLONES if (GIT_ROOT / c / ".git").exists()]
    if len(clones) < 2:
        pytest.skip("lobes-cli clones not present under ~/git")
    repos = set()
    for clone in clones:
        remote = _git(clone, "remote", "get-url", "origin").strip()
        r = client.post("/api/repodocs", json=_body(remote=remote, path="docs/specs/probe.md"))
        repos.add(r.json()["doc"]["repo"])
    assert repos == {"github.com/agentculture/lobes-cli"}
    assert len(client.get("/api/repodocs").json()["docs"]) == 1
