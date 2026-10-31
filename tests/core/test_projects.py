"""Projects: domains and connected projects (plan task t2)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from culture_library.core import Store, ValidationError
from culture_library.core.projects import Projects

LIB = "github.com/agentculture/culture-library"
CULTURE = "github.com/agentculture/culture"
DARIA = "github.com/agentculture/daria"


@pytest.fixture
def projects(store: Store) -> Projects:
    return Projects(store)


@pytest.fixture
def three(projects: Projects) -> Projects:
    projects.upsert(LIB, ["media", "search"], [CULTURE])
    projects.upsert(CULTURE, ["mesh"], [LIB, DARIA])
    projects.upsert(DARIA, ["mesh", "media"], [])
    return projects


def test_filters_exact_sets(three: Projects) -> None:
    assert three.repos_for(domain="media") == sorted([LIB, DARIA])
    assert three.repos_for(domain="mesh") == sorted([CULTURE, DARIA])
    assert three.repos_for(domain="search") == [LIB]
    assert three.with_connected(LIB) == sorted([LIB, CULTURE])
    assert three.with_connected(CULTURE) == sorted([CULTURE, LIB, DARIA])
    assert three.with_connected(DARIA) == [DARIA]
    assert three.repos_for() == sorted([LIB, CULTURE, DARIA])


def test_unknown_domain_and_project_are_empty_or_self(three: Projects) -> None:
    assert three.repos_for(domain="nope") == []
    assert three.with_connected("github.com/x/unknown") == ["github.com/x/unknown"]


def test_upsert_replaces_both_lists(projects: Projects) -> None:
    projects.upsert(LIB, ["a", "b"], [CULTURE])
    projects.upsert(LIB, ["c"], [DARIA])
    assert projects.repos_for(domain="a") == []
    assert projects.repos_for(domain="c") == [LIB]
    assert projects.with_connected(LIB) == sorted([LIB, DARIA])
    assert projects.get(LIB)["domains"] == ["c"]


def test_connected_normalized(projects: Projects) -> None:
    projects.upsert(DARIA, [], ["https://github.com/agentculture/culture-library.git"])
    projects.upsert(CULTURE, [], ["github.com/agentculture/culture-library"])
    assert projects.get(DARIA)["connected"] == [LIB]
    assert projects.get(CULTURE)["connected"] == [LIB]
    assert projects.with_connected("https://github.com/agentculture/daria.git") == sorted(
        [DARIA, LIB]
    )


def test_repo_normalized_and_validation(projects: Projects) -> None:
    projects.upsert("https://github.com/agentculture/daria.git", ["x", "x"], [])
    assert projects.get(DARIA)["domains"] == ["x"]
    with pytest.raises(ValidationError):
        projects.upsert("", [], [])
    with pytest.raises(ValidationError):
        projects.upsert(LIB, "media", [])  # type: ignore[arg-type]


def test_stdlib_only() -> None:
    src = Path(__file__).resolve().parents[2] / "culture_library" / "core" / "projects.py"
    mods = set()
    for node in ast.walk(ast.parse(src.read_text())):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and not node.level:
            mods.add((node.module or "").split(".")[0])
    assert mods <= {"__future__", "json", "typing"}


def test_org_repo_shorthand_means_github(projects: Projects) -> None:
    """Review finding 2: ``org/repo`` (no scheme, no dot in the first segment) is GitHub."""
    from culture_library.core.projects import _norm

    assert _norm("agentculture/daria") == DARIA
    assert _norm("agentculture/daria.git") == DARIA
    assert _norm("gitlab.com/agentculture/daria") == "gitlab.com/agentculture/daria"
    assert _norm("example.org/daria") == "example.org/daria"
    assert _norm("spark:/home/spark/git/x") == "spark:/home/spark/git/x"
    projects.upsert(LIB, [], ["agentculture/daria"])
    assert projects.get(LIB)["connected"] == [DARIA]
