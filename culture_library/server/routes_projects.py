"""Project and domain index: ``GET /api/projects`` and ``GET /api/domains``.

A project is a repo (decision c27). Its domains and connected projects come from the repo's
culture.yaml, pushed by culture-rules with each repo document (``core/projects.py``). These
routes list them for the site's browse views and the MCP tools (deviation d18).

Only projects with at least one doc the caller can see are listed (removed docs don't count),
so a private repo's name and domains never reach a caller without private access. Every
migrated doc is private (decision c31), so in practice that means a ``private`` principal.
"""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends

from culture_library.core import Store
from culture_library.core.projects import Projects
from culture_library.core.repodocs import RepoDocs
from culture_library.server.auth import Principal, check, current_principal, get_store
from culture_library.server.routes_library import FlatErrorRoute

router = APIRouter(tags=["repodocs"], route_class=FlatErrorRoute)


def _visible_counts(store: Store, principal: Principal) -> Counter:
    docs = RepoDocs(store).list(include_private=principal.private)
    return Counter(d.repo for d in docs)


@router.get("/api/projects")
def list_projects(
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
) -> dict:
    check(principal, "read")
    counts = _visible_counts(store, principal)
    projects = [
        {**p, "doc_count": counts[p["repo"]]} for p in Projects(store).list() if counts[p["repo"]]
    ]
    return {"projects": projects}


@router.get("/api/domains")
def list_domains(
    principal: Principal = Depends(current_principal),
    store: Store = Depends(get_store),
) -> dict:
    check(principal, "read")
    counts = _visible_counts(store, principal)
    by_domain: dict[str, list[str]] = {}
    for p in Projects(store).list():
        if not counts[p["repo"]]:
            continue
        for domain in p["domains"]:
            by_domain.setdefault(domain, []).append(p["repo"])
    return {
        "domains": [
            {"domain": d, "projects": sorted(repos)} for d, repos in sorted(by_domain.items())
        ]
    }
