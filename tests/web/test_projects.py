"""Browse by project and by domain (plan task t20): ``hashroute.js`` project/domain routes and
the pure ``projects-model.js`` (slug join, filters) under Node, fed with real API output.

Skipped when Node is not installed; the modules have no DOM and no imports.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.web.conftest import A, Site
from tests.web.test_evals_model import STATIC, _js
from tests.web.test_evals_model import pytestmark as _node_mark

pytestmark = _node_mark

KEYS = "k => k === 'doc-1'"
ORG = "https://github.com/agentculture/"
# 3 projects in 2 domains: alpha (build), beta (build, ops), gamma (ops).
PROJECTS = {
    "alpha": (["build"], ["https://github.com/agentculture/beta"]),
    "beta": (["build", "ops"], []),
    "gamma": (["ops"], []),
}
PATHS = {
    "alpha": [
        "docs/specs/2026-10-01-feed.md",
        "docs/plans/2026-10-02-feed.md",
        "docs/plans/2026-10-02-feed-split.md",
        "docs/deliveries/2026-10-05-feed.md",
        ".devague/frames/feed.json",
        "docs/reviews/notes.md",
    ],
    "beta": ["docs/specs/2026-10-03-search.md", "docs/plans/2026-10-04-search.md"],
    "gamma": ["docs/specs/2026-10-06-ops-run.md"],
}


@pytest.mark.parametrize(
    "hash_,want",
    [
        ("#projects", {"view": "projects", "repo": None}),
        (
            "#projects/github.com%2Fagentculture%2Falpha",
            {"view": "projects", "repo": "github.com/agentculture/alpha"},
        ),
        ("#domains", {"view": "domains", "domain": None}),
        ("#domains/build", {"view": "domains", "domain": "build"}),
        ("#projectsx", {"view": "home"}),
        ("#doc-1", {"view": "read", "key": "doc-1"}),
    ],
)
def test_browse_routes(hash_: str, want: dict) -> None:
    assert _js("hashroute.js", f"m.parseHash({json.dumps(hash_)}, {KEYS})") == want


def test_browse_hashes_round_trip() -> None:
    got = _js(
        "hashroute.js",
        "[m.projectsHash(), m.projectsHash('github.com/a/b'),"
        " m.domainsHash(), m.domainsHash('x y')]",
    )
    assert got == ["#projects", "#projects/github.com%2Fa%2Fb", "#domains", "#domains/x%20y"]


@pytest.mark.parametrize(
    "path,want",
    [
        ("docs/specs/2026-10-01-feed.md", {"slug": "feed", "role": "spec"}),
        ("docs/plans/2026-10-02-feed.md", {"slug": "feed", "role": "plan"}),
        ("docs/plans/2026-10-02-feed-split.md", {"slug": "feed", "role": "split"}),
        ("docs/deliveries/2026-10-05-feed.md", {"slug": "feed", "role": "delivery"}),
        (".devague/frames/feed.json", {"slug": "feed", "role": "frame"}),
        (".devague/plans/feed.json", {"slug": "feed", "role": "plan-state"}),
        (".devague/deliveries/feed.json", {"slug": "feed", "role": "delivery-state"}),
        ("docs/reviews/2026-10-01-feed.md", None),
        ("docs/specs/notes.md", None),
        (".devague/frames/a/b.json", None),
    ],
)
def test_slug_of(path: str, want) -> None:
    got = _js("projects-model.js", f"m.slugOf({json.dumps(path)})")
    assert (got and {k: got[k] for k in ("slug", "role")}) == want


def _doc(id_: int, path: str, repo: str = "r", **kw) -> dict:
    return {"id": id_, "repo": repo, "path": path, "removed_at": None, **kw}


def test_group_joins_on_slug_not_filename() -> None:
    # Different dates and file names, one slug: one group. A removed doc is left out.
    docs = [
        _doc(1, "docs/specs/2026-10-01-feed.md"),
        _doc(2, "docs/plans/2026-11-09-feed.md"),
        _doc(3, "docs/deliveries/2026-12-01-feed.md"),
        _doc(4, ".devague/frames/feed.json"),
        _doc(5, "docs/specs/2026-10-01-other.md"),
        _doc(6, "docs/plans/2026-10-09-other.md", removed_at="2026-10-10T00:00:00Z"),
        _doc(7, "docs/reviews/feed-review.md"),
    ]
    got = _js("projects-model.js", f"m.groupBySlug({json.dumps(docs)})")
    groups = {g["slug"]: [(d["role"], d["doc"]["id"]) for d in g["docs"]] for g in got["groups"]}
    assert groups == {
        "feed": [("spec", 1), ("plan", 2), ("delivery", 3), ("frame", 4)],
        "other": [("spec", 5)],
    }
    assert [d["id"] for d in got["other"]] == [7]


def _push(site: Site, name: str, path: str) -> None:
    domains, connected = PROJECTS[name]
    body = {
        "remote": ORG + name,
        "path": path,
        "commit": A,
        "committed_at": "2026-10-23T23:10:00Z",
        "default_branch_head": A,
        "visibility": "public",
        "content": f"# {path}\n",
        "project": {"domains": domains, "connected": connected},
    }
    r = site.c.post("/api/repodocs", json=body)
    assert r.status_code in (200, 201), r.text


def _paths(docs: list[dict]) -> set[str]:
    return {f"{re.sub('.*/', '', d['repo'])}:{d['path']}" for d in docs}


def test_three_projects_in_two_domains_show_exactly_the_expected_documents(site: Site) -> None:
    for name, paths in PATHS.items():
        for path in paths:
            _push(site, name, path)
    get = site.c.get
    projects = get("/api/projects").json()
    domains = get("/api/domains").json()
    every = {f"{n}:{p}" for n, ps in PATHS.items() for p in ps}

    # #projects: each project with its domains and doc count; #domains: each with its projects.
    rows = _js("projects-model.js", f"m.projectRows({json.dumps(projects)})")
    assert {r["name"]: (r["domains"], r["doc_count"]) for r in rows} == {
        "alpha": (["build"], 6),
        "beta": (["build", "ops"], 2),
        "gamma": (["ops"], 1),
    }
    drows = _js("projects-model.js", f"m.domainRows({json.dumps(domains)})")
    assert {r["domain"]: r["projects"] for r in drows} == {
        "build": ["alpha", "beta"],
        "ops": ["beta", "gamma"],
    }

    # Filters come from GET /api/repodocs: domain, project, project + connected.
    by_domain = {d: get(f"/api/repodocs?domain={d}").json()["docs"] for d in ("build", "ops")}
    assert _paths(by_domain["build"]) == {e for e in every if e.split(":")[0] in ("alpha", "beta")}
    assert _paths(by_domain["ops"]) == {e for e in every if e.split(":")[0] in ("beta", "gamma")}
    alpha = get(f"/api/repodocs?project={ORG[8:]}alpha").json()["docs"]
    assert _paths(alpha) == {e for e in every if e.startswith("alpha:")}
    both = get(f"/api/repodocs?project={ORG[8:]}alpha&connected=1").json()["docs"]
    assert _paths(both) == {e for e in every if e.split(":")[0] in ("alpha", "beta")}

    # The project page: alpha's feed slug groups spec, plan, split, delivery and frame together;
    # the review lands in Other; the connected project's docs list under it, not under alpha.
    page = _js(
        "projects-model.js",
        f"m.projectPage({json.dumps(both)}, {json.dumps(ORG[8:] + 'alpha')},"
        f" {json.dumps(projects)})",
    )
    own = page["own"]
    assert [g["slug"] for g in own["groups"]] == ["feed"]
    assert [d["role"] for d in own["groups"][0]["docs"]] == [
        "spec",
        "plan",
        "split",
        "delivery",
        "frame",
    ]
    assert [d["path"] for d in own["other"]] == ["docs/reviews/notes.md"]
    assert [c["repo"] for c in page["connected"]] == [ORG[8:] + "beta"]
    assert sorted(g["slug"] for g in page["connected"][0]["groups"]) == ["search"]
    # A domain page: projects in the domain, each with its own groups.
    dom = _js(
        "projects-model.js",
        f"m.domainPage({json.dumps(by_domain['ops'])}, 'ops', {json.dumps(projects)})",
    )
    assert {p["name"]: sorted(g["slug"] for g in p["groups"]) for p in dom} == {
        "beta": ["search"],
        "gamma": ["ops-run"],
    }


def test_static_files_are_wired() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'href="/static/projects.css"' in html
    assert 'id="view-projects"' in html
    assert (STATIC / "projects.js").is_file()
