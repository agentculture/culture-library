"""POST/GET /api/repodocs for devague work (plan task t10).

Working drafts, tombstones, base64/binary content and the size cap, project scope
(domains/connected), spec links, the all-private rule, and the mirror outbox + feed event
written in the push's own transaction.
"""

from __future__ import annotations

import base64
import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store  # noqa: E402
from culture_library.core.speclinks import SpecLinks  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402
from culture_library.server import routes_repodocs  # noqa: E402
from culture_library.server.auth import Principal, current_principal  # noqa: E402

A = "a" * 40
B = "b" * 40
C = "c" * 40
REMOTE = "https://github.com/agentculture/devague"


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


def _store(app) -> Store:
    return app.state.store


def _body(**kw) -> dict:
    body = dict(
        remote=REMOTE,
        path="docs/specs/x.md",
        commit=A,
        committed_at="2026-10-01T00:00:00Z",
        default_branch_head=A,
        content="hello\n",
    )
    body.update(kw)
    return {k: v for k, v in body.items() if v is not None}


def _working(**kw) -> dict:
    body = dict(remote=REMOTE, path="docs/specs/x.md", commit="working", content="draft\n")
    body.update(kw)
    return body


def _rows(app, table: str) -> list:
    with _store(app).reader() as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]  # nosec B608


def _count(app, table: str) -> int:
    return len(_rows(app, table))


def _repodoc_events(app) -> list[dict]:
    return [r for r in _rows(app, "feed") if r["subject"] == "repodoc"]


def _ok(r) -> dict:
    assert r.status_code in (200, 201), r.text
    return r.json()


# --- working drafts, outbox and feed --------------------------------------------------


def test_working_push_needs_no_commit_metadata(client, app) -> None:
    data = _ok(client.post("/api/repodocs", json=_working()))
    assert data["created"] is True
    assert data["version"] is None
    assert data["draft"]["sha256"]
    assert data["draft"]["ahead"] is True
    assert data["doc"]["kind"] == "spec"
    assert _count(app, "repodoc_versions") == 0
    doc_id = data["doc"]["id"]
    got = client.get(f"/api/repodocs/{doc_id}/content", params={"commit": "working"})
    assert got.content == b"draft\n"


def test_each_version_and_draft_enqueues_and_appends_one_event(client, app) -> None:
    v = _ok(client.post("/api/repodocs", json=_body()))
    assert v["created"] is True
    d = _ok(client.post("/api/repodocs", json=_working()))
    outbox = {r["key"]: r for r in _rows(app, "mirror_outbox")}
    repo, path = "github.com/agentculture/devague", "docs/specs/x.md"
    assert set(outbox) == {f"{repo}/{path}@{A}", f"{repo}/{path}@working"}
    assert outbox[f"{repo}/{path}@{A}"]["sha256"] == v["version"]["sha256"]
    assert outbox[f"{repo}/{path}@working"]["sha256"] == d["draft"]["sha256"]
    assert outbox[f"{repo}/{path}@{A}"]["content_type"].startswith("text/markdown")
    events = _repodoc_events(app)
    assert len(events) == 2
    assert [e["item_id"] for e in events] == [v["doc"]["id"]] * 2
    assert [e["sha256"] for e in events] == [v["version"]["sha256"], d["draft"]["sha256"]]
    assert all(e["visibility"] == "private" and e["kind"] == "spec" for e in events)
    assert [e["data"]["commit"] for e in events] == [A, "working"]  # jsonb: a dict


def test_feed_event_is_readable_via_the_feed_route(client) -> None:
    _ok(client.post("/api/repodocs", json=_body()))
    events = client.get("/api/feed", params={"subject": "repodoc"}).json()["events"]
    assert len(events) == 1
    assert events[0]["data"] == {
        "repo": "github.com/agentculture/devague",
        "path": "docs/specs/x.md",
        "commit": A,
    }


def test_noop_repush_appends_neither(client, app) -> None:
    _ok(client.post("/api/repodocs", json=_body()))
    again = client.post("/api/repodocs", json=_body())
    assert again.status_code == 200
    assert again.json()["created"] is False
    _ok(client.post("/api/repodocs", json=_working()))
    same = client.post("/api/repodocs", json=_working())
    assert same.status_code == 200
    assert same.json()["created"] is False
    assert _count(app, "mirror_outbox") == 2
    assert len(_repodoc_events(app)) == 2
    changed = _ok(client.post("/api/repodocs", json=_working(content="draft 2\n")))
    assert changed["created"] is True
    assert _count(app, "mirror_outbox") == 2  # the @working key is re-queued in place
    assert len(_repodoc_events(app)) == 3
    working = [r for r in _rows(app, "mirror_outbox") if r["key"].endswith("@working")][0]
    assert working["sha256"] == changed["draft"]["sha256"]


def test_feed_failure_rolls_back_the_version_and_outbox(app, monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("feed down")

    monkeypatch.setattr(routes_repodocs.feed, "append_repodoc", boom)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post("/api/repodocs", json=_body()).status_code == 500
    assert client.post("/api/repodocs", json=_working()).status_code == 500
    for table in ("repodoc_versions", "repodoc_drafts", "mirror_outbox", "feed"):
        assert _count(app, table) == 0, table


# --- tombstones ------------------------------------------------------------------------


def test_tombstone_hides_doc_keeps_history_drops_draft(client, app) -> None:
    doc_id = _ok(client.post("/api/repodocs", json=_body()))["doc"]["id"]
    _ok(client.post("/api/repodocs", json=_working()))
    outbox_before = _count(app, "mirror_outbox")
    r = client.post(
        "/api/repodocs",
        json={"remote": REMOTE, "path": "docs/specs/x.md", "commit": B, "tombstone": True},
    )
    data = _ok(r)
    assert r.status_code == 200
    assert data["tombstoned"] is True
    assert data["created"] is False
    assert data["doc"]["removed_commit"] == B
    assert client.get("/api/repodocs").json()["docs"] == []
    removed = client.get("/api/repodocs", params={"include_removed": 1}).json()["docs"]
    assert [d["id"] for d in removed] == [doc_id]
    assert client.get(f"/api/repodocs/{doc_id}/content", params={"commit": A}).content == b"hello\n"
    assert (
        client.get(f"/api/repodocs/{doc_id}/content", params={"commit": "working"}).status_code
        == 404
    )
    assert _count(app, "mirror_outbox") == outbox_before  # nothing leaves (or is added to) S3
    events = _repodoc_events(app)
    assert [e["type"] for e in events] == ["updated", "updated", "archived"]
    # a replayed tombstone is a no-op: no second event
    again = client.post(
        "/api/repodocs",
        json={"remote": REMOTE, "path": "docs/specs/x.md", "commit": B, "tombstone": True},
    )
    assert again.status_code == 200
    assert again.json()["tombstoned"] is False
    assert len(_repodoc_events(app)) == 3


def test_tombstone_of_a_never_committed_draft(client) -> None:
    _ok(client.post("/api/repodocs", json=_working(path=".devague/frames/f.json")))
    r = client.post(
        "/api/repodocs",
        json={
            "remote": REMOTE,
            "path": ".devague/frames/f.json",
            "commit": "working",
            "tombstone": True,
        },
    )
    assert _ok(r)["doc"]["removed_commit"] == "working"
    assert client.get("/api/repodocs").json()["docs"] == []


def test_tombstone_of_unknown_doc_is_404(client, app) -> None:
    r = client.post(
        "/api/repodocs",
        json={"remote": REMOTE, "path": "docs/specs/none.md", "commit": A, "tombstone": True},
    )
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"
    assert _count(app, "repodocs") == 0


def test_tombstone_needs_write_scope_for_the_kind(app, client) -> None:
    _ok(client.post("/api/repodocs", json=_body()))
    app.dependency_overrides[current_principal] = lambda: _principal("write", kinds={"plan"})
    r = client.post(
        "/api/repodocs",
        json={"remote": REMOTE, "path": "docs/specs/x.md", "commit": B, "tombstone": True},
    )
    assert r.status_code == 403


# --- content, binary, size cap ---------------------------------------------------------


def test_png_round_trips_byte_identical(client, app) -> None:
    png = b"\x89PNG\r\n\x1a\n" + os.urandom(1_300_000)
    body = _body(path="docs/design/reference/shot.png", content=None)
    body["content_b64"] = base64.b64encode(png).decode("ascii")
    data = _ok(client.post("/api/repodocs", json=body))
    assert data["doc"]["kind"] == "design"
    assert data["version"]["size"] == len(png)
    got = client.get(f"/api/repodocs/{data['doc']['id']}/content")
    assert got.status_code == 200
    assert got.content == png
    outbox = _rows(app, "mirror_outbox")
    assert outbox[0]["content_type"] == "application/octet-stream"


def test_content_and_content_b64_are_exclusive(client) -> None:
    both = _body(content_b64=base64.b64encode(b"x").decode())
    assert client.post("/api/repodocs", json=both).status_code == 422
    neither = _body(content=None)
    assert client.post("/api/repodocs", json=neither).status_code == 422
    bad = _body(content=None, content_b64="not base64!!")
    r = client.post("/api/repodocs", json=bad)
    assert r.status_code == 400
    assert r.json()["code"] == "invalid"


def _nothing_stored(app) -> None:
    for table in ("repodocs", "repodoc_versions", "repodoc_drafts", "mirror_outbox", "feed"):
        assert _count(app, table) == 0, table
    files = _store(app).files_dir
    assert not files.exists() or not any(p.is_file() for p in files.rglob("*"))


def test_over_the_limit_is_413_and_stores_nothing(client, app, monkeypatch) -> None:
    monkeypatch.setenv("CULTURE_LIBRARY_REPODOC_MAX_BYTES", "1000")
    r = client.post("/api/repodocs", json=_body(content="x" * 1001))
    assert r.status_code == 413
    assert r.json()["code"] == "too_large"
    b64 = _body(content=None, content_b64=base64.b64encode(b"y" * 1001).decode())
    assert client.post("/api/repodocs", json=b64).json()["code"] == "too_large"
    work = client.post("/api/repodocs", json=_working(content="z" * 1001))
    assert work.status_code == 413
    _nothing_stored(app)
    assert _ok(client.post("/api/repodocs", json=_body(content="x" * 1000)))["created"]


def test_body_far_over_the_limit_is_refused_while_read(client, app, monkeypatch) -> None:
    monkeypatch.setenv("CULTURE_LIBRARY_REPODOC_MAX_BYTES", "1000")
    r = client.post("/api/repodocs", json=_body(content="x" * 500_000))
    assert r.status_code == 413
    assert r.json() == {"detail": r.json()["detail"], "code": "too_large"}
    _nothing_stored(app)


def test_default_limit_is_16_mib(monkeypatch) -> None:
    monkeypatch.delenv("CULTURE_LIBRARY_REPODOC_MAX_BYTES", raising=False)
    assert routes_repodocs.max_content_bytes() == 16 * 1024 * 1024
    monkeypatch.setenv("CULTURE_LIBRARY_REPODOC_MAX_BYTES", "2048")
    assert routes_repodocs.max_content_bytes() == 2048
    monkeypatch.setenv("CULTURE_LIBRARY_REPODOC_MAX_BYTES", "nonsense")
    assert routes_repodocs.max_content_bytes() == 16 * 1024 * 1024


# --- all-private -------------------------------------------------------------------------


def test_every_push_is_private_whatever_the_body_says(app, client) -> None:
    pub = _ok(client.post("/api/repodocs", json=_body(visibility="public")))
    assert pub["doc"]["visibility"] == "private"
    assert pub["version"]["visibility"] == "private"
    w = _ok(
        client.post("/api/repodocs", json=_working(path="docs/plans/p.md", visibility="public"))
    )
    assert w["draft"]["visibility"] == "private"
    assert w["doc"]["visibility"] == "private"
    assert all(e["visibility"] == "private" for e in _repodoc_events(app))
    app.dependency_overrides[current_principal] = lambda: _principal("read")
    assert client.get("/api/repodocs").json()["docs"] == []
    assert client.get("/api/repodocs", params={"include_removed": 1}).json()["docs"] == []
    assert client.get(f"/api/repodocs/{pub['doc']['id']}").status_code == 404
    assert client.get("/api/feed", params={"subject": "repodoc"}).json()["events"] == []
    app.dependency_overrides[current_principal] = lambda: _principal("read", private=True)
    assert len(client.get("/api/repodocs").json()["docs"]) == 2


# --- same (remote, path, commit) from two checkouts --------------------------------------


def test_two_checkout_paths_yield_one_doc_and_one_version(client, app) -> None:
    clone = _body(remote="git@github.com:agentculture/devague.git")
    clone["checkout"] = "/srv/git/devague"
    worktree = _body(remote="https://github.com/agentculture/devague.git")
    worktree["checkout"] = "/srv/git/.worktrees.devague/t1"
    first = _ok(client.post("/api/repodocs", json=clone))
    second = _ok(client.post("/api/repodocs", json=worktree))
    assert first["doc"]["id"] == second["doc"]["id"]
    assert _count(app, "repodocs") == 1
    assert _count(app, "repodoc_versions") == 1
    assert _count(app, "mirror_outbox") == 1
    assert len(_repodoc_events(app)) == 1
    _ok(client.post("/api/repodocs", json=_working(remote=clone["remote"])))
    _ok(client.post("/api/repodocs", json=_working(remote=worktree["remote"])))
    assert _count(app, "repodoc_drafts") == 1


# --- project scope and listing filters ---------------------------------------------------

R1 = "https://github.com/agentculture/alpha"
R2 = "git@github.com:agentculture/beta.git"
R3 = "https://github.com/agentculture/gamma.git"


def _seed_projects(client) -> dict[str, int]:
    """alpha: {mesh}, connected to gamma; beta: {mesh, rag}; gamma: {rag}."""
    projects = {
        R1: {"domains": ["mesh"], "connected": ["github.com/agentculture/gamma"]},
        R2: {"domains": ["mesh", "rag"], "connected": []},
        R3: {"domains": ["rag"], "connected": []},
    }
    ids = {}
    for remote, project in projects.items():
        for path in ("docs/specs/s.md", "docs/plans/p.md"):
            data = _ok(
                client.post("/api/repodocs", json=_body(remote=remote, path=path, project=project))
            )
            ids[f"{data['doc']['repo'].rsplit('/', 1)[-1]}:{path}"] = data["doc"]["id"]
    return ids


def _listed(client, **params) -> set[str]:
    r = client.get("/api/repodocs", params=params)
    assert r.status_code == 200, r.text
    return {f"{d['repo'].rsplit('/', 1)[-1]}:{d['path']}" for d in r.json()["docs"]}


def _of(*repos: str) -> set[str]:
    return {f"{r}:{p}" for r in repos for p in ("docs/specs/s.md", "docs/plans/p.md")}


def test_project_upsert_is_returned(client) -> None:
    data = _ok(
        client.post(
            "/api/repodocs",
            json=_body(project={"domains": ["mesh"], "connected": [R3]}),
        )
    )
    assert data["project"] == {
        "repo": "github.com/agentculture/devague",
        "domains": ["mesh"],
        "connected": ["github.com/agentculture/gamma"],
    }
    assert _ok(client.post("/api/repodocs", json=_working()))["project"] is None


def test_domain_project_and_connected_filters(client) -> None:
    _seed_projects(client)
    assert _listed(client) == _of("alpha", "beta", "gamma")
    assert _listed(client, domain="mesh") == _of("alpha", "beta")
    assert _listed(client, domain="rag") == _of("beta", "gamma")
    assert _listed(client, domain="nope") == set()
    assert _listed(client, project=R1) == _of("alpha")
    assert _listed(client, project="github.com/agentculture/alpha") == _of("alpha")
    assert _listed(client, project=R1, connected=1) == _of("alpha", "gamma")
    assert _listed(client, project=R2, connected=1) == _of("beta")
    assert _listed(client, project=R1, connected=1, domain="rag") == _of("gamma")
    assert _listed(client, domain="mesh", path="docs/plans/p.md") == {
        "alpha:docs/plans/p.md",
        "beta:docs/plans/p.md",
    }


def test_include_removed_filter(client) -> None:
    _seed_projects(client)
    _ok(
        client.post(
            "/api/repodocs",
            json={"remote": R2, "path": "docs/plans/p.md", "commit": B, "tombstone": True},
        )
    )
    assert _listed(client, domain="mesh") == _of("alpha", "beta") - {"beta:docs/plans/p.md"}
    assert _listed(client, domain="mesh", include_removed=1) == _of("alpha", "beta")


def test_connected_needs_project(client) -> None:
    r = client.get("/api/repodocs", params={"connected": 1})
    assert r.status_code == 400
    assert r.json()["code"] == "invalid"


def test_latest_project_push_wins(client) -> None:
    _seed_projects(client)
    _ok(client.post("/api/repodocs", json=_body(remote=R3, project={"domains": ["mesh"]})))
    assert _listed(client, domain="rag") == _of("beta")
    assert "gamma:docs/specs/s.md" in _listed(client, domain="mesh")


def test_bad_project_is_refused_before_anything_is_stored(client, app) -> None:
    r = client.post("/api/repodocs", json=_body(project={"domains": "x"}))
    assert r.status_code == 422
    r = client.post("/api/repodocs", json=_body(project={"connected": [7]}))
    assert r.status_code == 422
    _nothing_stored(app)


def test_bad_connected_entry_is_dropped_with_a_warning(client, app) -> None:
    """Review finding 2: one bad ``connected`` entry must not block the repo's docs."""
    r = client.post(
        "/api/repodocs",
        json=_body(project={"domains": ["x"], "connected": ["/abs", R3, "agentculture/beta"]}),
    )
    assert r.status_code == 201
    data = r.json()
    assert data["project"]["connected"] == [
        "github.com/agentculture/gamma",
        "github.com/agentculture/beta",
    ]
    assert len(data["warnings"]) == 1
    assert data["warnings"][0].startswith("connected '/abs' ignored: ")
    assert _rows(app, "repodoc_versions")
    clean = _ok(client.post("/api/repodocs", json=_body(project={"connected": [R3]})))
    assert "warnings" not in clean
    assert "warnings" not in _ok(client.post("/api/repodocs", json=_working()))


# --- spec links ---------------------------------------------------------------------------


def _links(**kw) -> dict:
    links = dict(
        pr={"url": "https://github.com/agentculture/devague/pull/7", "number": 7},
        merge_commit=C,
        code_paths=["culture_library/core/repodocs.py"],
        at_commit=C,
        at_committed_at="2026-10-02T00:00:00Z",
        trailer_commits=[B],
    )
    links.update(kw)
    return links


def test_spec_links_are_stored_and_follow_renames(client, app) -> None:
    data = _ok(client.post("/api/repodocs", json=_body(spec_links=_links())))
    assert data["spec_links"]["merge_commit"] == C
    assert data["spec_links"]["pr"] == {
        "url": "https://github.com/agentculture/devague/pull/7",
        "number": 7,
    }
    doc_id = data["doc"]["id"]
    renamed = _links(
        code_paths=["culture_library/core/repodocs2.py"],
        at_commit=B,
        at_committed_at="2026-10-03T00:00:00Z",
    )
    _ok(client.post("/api/repodocs", json=_body(spec_links=renamed)))
    stale = _links(code_paths=["old.py"], at_commit=A, at_committed_at="2026-09-01T00:00:00Z")
    _ok(client.post("/api/repodocs", json=_body(spec_links=stale)))
    links = SpecLinks(_store(app)).links(doc_id)
    assert links["code_paths"] == ["culture_library/core/repodocs2.py"]
    assert links["at_commit"] == B


def test_push_says_whether_spec_links_were_applied(client) -> None:
    """Review finding 4: an older (not stored) code-path push says so; equal time wins."""
    first = _ok(client.post("/api/repodocs", json=_body(spec_links=_links())))
    assert first["spec_links_applied"] is True
    stale = _links(code_paths=["old.py"], at_commit=A, at_committed_at="2026-09-01T00:00:00Z")
    data = _ok(client.post("/api/repodocs", json=_body(spec_links=stale)))
    assert data["spec_links_applied"] is False
    assert data["spec_links"]["code_paths"] == ["culture_library/core/repodocs.py"]
    same_time = _links(code_paths=["new.py"], at_commit=B)
    data = _ok(client.post("/api/repodocs", json=_body(spec_links=same_time)))
    assert data["spec_links_applied"] is True
    assert data["spec_links"]["code_paths"] == ["new.py"]
    assert "spec_links_applied" not in _ok(client.post("/api/repodocs", json=_body()))


def test_spec_links_only_on_specs_and_validated_up_front(client, app) -> None:
    r = client.post("/api/repodocs", json=_body(path="docs/plans/p.md", spec_links=_links()))
    assert r.status_code == 400
    assert r.json()["code"] == "invalid"
    r = client.post("/api/repodocs", json=_body(spec_links=_links(at_committed_at=None)))
    assert r.status_code == 422
    r = client.post("/api/repodocs", json=_body(spec_links=_links(at_committed_at="yesterday")))
    assert r.status_code == 422
    _nothing_stored(app)


def test_spec_links_on_a_tombstone_are_refused(client) -> None:
    _ok(client.post("/api/repodocs", json=_body()))
    r = client.post(
        "/api/repodocs",
        json={
            "remote": REMOTE,
            "path": "docs/specs/x.md",
            "commit": B,
            "tombstone": True,
            "spec_links": _links(),
        },
    )
    assert r.status_code == 400


# --- the old body shape still works ------------------------------------------------------


def test_old_body_shape_is_still_valid(client) -> None:
    old = dict(
        remote=REMOTE,
        path="docs/specs/old.md",
        commit=A,
        committed_at="2026-10-01T00:00:00Z",
        default_branch_head=A,
        visibility="private",
        content="old\n",
    )
    r = client.post("/api/repodocs", json=old)
    assert r.status_code == 201
    assert set(r.json()) >= {"doc", "version", "created", "current_changed"}


def test_commit_push_still_needs_commit_metadata(client) -> None:
    r = client.post("/api/repodocs", json=_body(committed_at=None))
    assert r.status_code == 400
    r = client.post("/api/repodocs", json=_body(default_branch_head=None))
    assert r.status_code == 400
