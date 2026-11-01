"""Repo documents: versioned identity, visibility and dedup (plan task t8).

The fixture docs under ``fixtures_repodocs/`` are synthetic (written for this test, not
copied from any repo); their remotes name five public agentculture repos, verified public
with ``gh repo view agentculture/<r> --json visibility`` on 2026-10-24.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from culture_library.core import Store, ValidationError
from culture_library.core.repodocs import (
    NotFoundError,
    RepoDocs,
    infer_kind,
    normalize_path,
    normalize_remote,
)

FIXTURES = Path(__file__).parent / "fixtures_repodocs"
PACKAGE = Path(__file__).resolve().parents[2] / "culture_library"

A = "a" * 40
B = "b" * 40
C = "c" * 40
F = "f" * 40
REMOTE = "https://github.com/agentculture/lobes-cli"


@pytest.fixture
def docs(store: Store) -> RepoDocs:
    return RepoDocs(store)


def _push(docs: RepoDocs, **kw):
    args = dict(
        remote=REMOTE,
        path="docs/specs/x.md",
        commit=A,
        committed_at="2026-10-01T00:00:00Z",
        default_branch_head=A,
        visibility="public",
        content="v-a\n",
    )
    args.update(kw)
    return docs.push(**args)


# --- remote / path / kind normalization ------------------------------------------------


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/agentculture/lobes-cli",
        "https://github.com/agentculture/lobes-cli.git",
        "https://github.com/agentculture/lobes-cli/",
        "http://github.com/agentculture/lobes-cli.git/",
        "https://user:secret-token@github.com/agentculture/lobes-cli.git",
        "git@github.com:agentculture/lobes-cli.git",
        "git@github.com:agentculture/lobes-cli",
        "ssh://git@github.com/agentculture/lobes-cli.git",
        "ssh://git@github.com:22/agentculture/lobes-cli",
        "git://github.com/agentculture/lobes-cli.git",
        "HTTPS://GitHub.com/AgentCulture/Lobes-CLI.git",
        "  https://github.com/agentculture/lobes-cli.git  ",
    ],
)
def test_remote_forms_collapse_to_one_id(remote: str) -> None:
    assert normalize_remote(remote) == "github.com/agentculture/lobes-cli"


def test_remote_never_keeps_credentials() -> None:
    assert "secret" not in normalize_remote("https://bot:secret@gitlab.example.com/g/r.git")
    assert normalize_remote("https://bot:secret@gitlab.example.com/g/r.git") == (
        "gitlab.example.com/g/r"
    )


@pytest.mark.parametrize(
    "remote,expected",
    [
        ("spark:/srv/git/transformer", "spark:/srv/git/transformer"),
        ("spark:/srv/git/transformer/", "spark:/srv/git/transformer"),
        ("Spark:~/git/transformer", "spark:~/git/transformer"),
    ],
)
def test_host_path_identity_for_repos_without_remote(remote: str, expected: str) -> None:
    assert normalize_remote(remote) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "/srv/git/transformer",
        "file:///srv/git/x",
        "https://github.com",
        "spark:",
        "https://github.com/../etc",
    ],
)
def test_bad_remote_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        normalize_remote(bad)


def test_path_normalization() -> None:
    assert normalize_path("./docs/specs/x.md") == "docs/specs/x.md"
    assert normalize_path("/docs//specs/x.md") == "docs/specs/x.md"
    assert normalize_path("docs\\specs\\x.md") == "docs/specs/x.md"
    for bad in ("", "../x.md", "docs/../../x", "docs/", "."):
        with pytest.raises(ValidationError):
            normalize_path(bad)


@pytest.mark.parametrize(
    "path,kind",
    [
        ("docs/specs/a.md", "spec"),
        ("docs/plans/a.md", "plan"),
        ("docs/deliveries/a.md", "devague-record"),
        (".devague/frames/a.json", "devague-record"),
        (".devague/plans/a.json", "devague-record"),
        ("README.md", "document"),
        ("docs/notes.md", "document"),
    ],
)
def test_infer_kind(path: str, kind: str) -> None:
    assert infer_kind(path) == kind


# --- criterion 1: identity, versions pinned to commits, current ------------------------


def test_push_creates_doc_with_current_version(docs: RepoDocs) -> None:
    r = _push(docs)
    assert r.created
    assert r.current_changed
    assert r.doc.repo == "github.com/agentculture/lobes-cli"
    assert r.doc.path == "docs/specs/x.md"
    assert r.doc.kind == "spec"
    assert r.doc.current_commit == A
    assert r.version.commit == A
    assert r.version.on_default_branch
    assert docs.content(r.doc.id, include_private=False) == b"v-a\n"


def test_newer_default_commit_becomes_current(docs: RepoDocs) -> None:
    first = _push(docs)
    r = _push(
        docs, commit=B, default_branch_head=B, committed_at="2026-10-02T00:00:00Z", content="v-b\n"
    )
    assert r.doc.id == first.doc.id
    assert r.current_changed
    assert r.doc.current_commit == B
    assert docs.content(r.doc.id, include_private=False) == b"v-b\n"
    assert docs.content(r.doc.id, commit=A, include_private=False) == b"v-a\n"
    assert [v.commit for v in docs.versions(r.doc.id, include_private=False)] == [B, A]


def test_older_commit_from_stale_clone_adds_history_only(docs: RepoDocs) -> None:
    _push(
        docs, commit=B, default_branch_head=B, committed_at="2026-10-02T00:00:00Z", content="v-b\n"
    )
    r = _push(docs)  # stale clone: its default head is the older commit A
    assert r.created
    assert not r.current_changed
    assert r.doc.current_commit == B
    assert docs.content(r.doc.id, include_private=False) == b"v-b\n"
    assert {v.commit for v in docs.versions(r.doc.id, include_private=False)} == {A, B}


def test_feature_branch_push_adds_history_only(docs: RepoDocs) -> None:
    _push(docs)
    r = _push(
        docs,
        commit=F,
        default_branch_head=A,
        committed_at="2026-10-09T00:00:00Z",
        content="feature\n",
    )
    assert r.created
    assert not r.current_changed
    assert not r.version.on_default_branch
    assert r.doc.current_commit == A
    assert docs.content(r.doc.id, include_private=False) == b"v-a\n"
    assert docs.content(r.doc.id, commit=F, include_private=False) == b"feature\n"


def test_default_branch_flag_is_a_boolean_that_only_rises(docs: RepoDocs, store: Store) -> None:
    """t11: on_default_branch is a Postgres boolean; a re-push sets it (GREATEST), a later
    re-push from a feature branch never clears it."""

    def stored() -> list:
        with store.reader() as conn:
            return [r[0] for r in conn.execute("SELECT on_default_branch FROM repodoc_versions")]

    _push(docs, commit=F, default_branch_head=A, content="feature\n")
    assert stored() == [False]
    merged = _push(docs, commit=F, default_branch_head=F, content="feature\n")
    assert not merged.created
    assert merged.version.on_default_branch is True
    assert stored() == [True]
    _push(docs, commit=F, default_branch_head=A, content="feature\n")
    assert stored() == [True]
    assert type(stored()[0]) is bool


def test_feature_branch_only_doc_has_no_current(docs: RepoDocs) -> None:
    r = _push(docs, commit=F, default_branch_head=A, content="feature\n")
    assert r.doc.current_commit is None
    # content() falls back to the newest version when there is no current one
    assert docs.content(r.doc.id, include_private=False) == b"feature\n"


def test_feature_commit_later_merged_becomes_current(docs: RepoDocs) -> None:
    _push(docs)
    _push(
        docs,
        commit=F,
        default_branch_head=A,
        committed_at="2026-10-09T00:00:00Z",
        content="feature\n",
    )
    r = _push(
        docs,
        commit=F,
        default_branch_head=F,
        committed_at="2026-10-09T00:00:00Z",
        content="feature\n",
    )  # fast-forwarded onto the default branch
    assert not r.created
    assert r.current_changed
    assert r.doc.current_commit == F


def test_committed_at_timezones_compare_as_instants(docs: RepoDocs) -> None:
    _push(docs, commit=A, default_branch_head=A, committed_at="2026-10-02T01:00:00+02:00")
    # 2026-10-01T23:30Z is later than 2026-10-01T23:00Z (= 01:00+02:00)
    r = _push(
        docs, commit=B, default_branch_head=B, committed_at="2026-10-01T23:30:00Z", content="v-b\n"
    )
    assert r.doc.current_commit == B


def test_paths_are_separate_docs(docs: RepoDocs) -> None:
    a = _push(docs, path="docs/specs/x.md")
    b = _push(docs, path="docs/specs/y.md")
    assert a.doc.id != b.doc.id


def test_push_validation(docs: RepoDocs) -> None:
    for bad in (
        dict(commit="abc"),
        dict(commit="z" * 40),
        dict(default_branch_head="nope"),
        dict(committed_at="yesterday"),
        dict(visibility="secret"),
        dict(content=None),
    ):
        with pytest.raises(ValidationError):
            _push(docs, **bad)


def test_sha256_object_format_commits_accepted(docs: RepoDocs) -> None:
    sha = "d" * 64
    assert _push(docs, commit=sha, default_branch_head=sha).doc.current_commit == sha


# --- criterion 2: clone/worktree dedup, visibility ------------------------------------


def test_same_commit_from_clone_and_worktree_is_one_version(docs: RepoDocs) -> None:
    one = _push(docs, remote="https://github.com/agentculture/lobes-cli")
    two = _push(docs, remote="git@github.com:agentculture/lobes-cli.git", commit=A.upper())
    assert two.doc.id == one.doc.id
    assert not two.created
    assert len(docs.versions(one.doc.id, include_private=True)) == 1
    assert len(docs.list(include_private=True)) == 1


def test_identical_content_in_two_repos_stays_two_docs(docs: RepoDocs) -> None:
    a = _push(docs, remote="https://github.com/agentculture/devague", content="same\n")
    b = _push(docs, remote="https://github.com/agentculture/pocketsync", content="same\n")
    assert a.doc.id != b.doc.id
    assert a.version.sha256 == b.version.sha256
    assert {d.repo for d in docs.list(include_private=False)} == {
        "github.com/agentculture/devague",
        "github.com/agentculture/pocketsync",
    }


def test_push_without_visibility_is_private(docs: RepoDocs) -> None:
    r = _push(docs, visibility=None)
    assert r.doc.visibility == "private"
    assert r.version.visibility == "private"
    assert docs.get(r.doc.id, include_private=False) is None
    assert docs.get(r.doc.id, include_private=True) is not None
    assert docs.list(include_private=False) == []
    with pytest.raises(NotFoundError):
        docs.content(r.doc.id, include_private=False)


def test_private_repo_doc_is_private(docs: RepoDocs) -> None:
    r = _push(docs, remote="https://github.com/agentculture/steward", visibility="private")
    assert r.doc.visibility == "private"
    assert (
        docs.find(
            "git@github.com:agentculture/steward.git", "docs/specs/x.md", include_private=False
        )
        is None
    )


def test_repush_can_tighten_but_not_loosen_visibility(docs: RepoDocs) -> None:
    _push(docs, visibility="public")
    r = _push(docs, visibility=None)  # same version re-pushed without visibility
    assert r.version.visibility == "private"
    assert r.doc.visibility == "private"
    r = _push(docs, visibility="public")
    assert r.version.visibility == "private"


def test_private_history_hidden_from_public_reader(docs: RepoDocs) -> None:
    _push(docs)
    r = _push(
        docs,
        commit=F,
        default_branch_head=A,
        visibility="private",
        committed_at="2026-10-09T00:00:00Z",
        content="secret\n",
    )
    assert [v.commit for v in docs.versions(r.doc.id, include_private=False)] == [A]
    assert len(docs.versions(r.doc.id, include_private=True)) == 2
    with pytest.raises(NotFoundError):
        docs.content(r.doc.id, commit=F, include_private=False)


# --- criterion 3: the library never writes to repos -----------------------------------

_EXEC_FUNCS = {
    "run",
    "call",
    "check_call",
    "check_output",
    "Popen",
    "getoutput",
    "getstatusoutput",
    "system",
    "popen",
    "execv",
    "execve",
    "execvp",
    "execvpe",
    "execl",
    "execlp",
    "spawnv",
    "spawnvp",
    "spawnl",
    "spawnlp",
    "posix_spawn",
    "posix_spawnp",
    "create_subprocess_exec",
    "create_subprocess_shell",
}
_FORBIDDEN_MODULES = {"git", "pygit2", "dulwich", "sh", "plumbum"}
_FORBIDDEN_TOOLS = re.compile(r"(^|[\s/'\"])(git|devague)(\s|$|['\"])")
_GIT_WRITE = re.compile(
    r"""["']git["']\s*,\s*["'](checkout|switch|commit|push|reset|merge|rebase|add|rm|mv|"""
    r"""restore|stash|tag|branch|worktree|clean|pull|fetch|clone|apply|am|cherry-pick)["']"""
)


def _strings(node: ast.AST) -> list[str]:
    return [
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def scan_source(source: str, name: str = "<src>") -> list[str]:
    """Return violations: process launches of git/devague, git-library imports, git writes."""
    found: list[str] = []
    tree = ast.parse(source, name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [node.module or ""]
        else:
            mods = []
        for mod in mods:
            if mod.split(".")[0] in _FORBIDDEN_MODULES:
                found.append(f"{name}:{node.lineno}: imports {mod}")
        if isinstance(node, ast.Call):
            func = node.func
            fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if fname in _EXEC_FUNCS:
                args = [
                    s for a in [*node.args, *(k.value for k in node.keywords)] for s in _strings(a)
                ]
                if any(_FORBIDDEN_TOOLS.search(f" {s} ") for s in args):
                    found.append(f"{name}:{node.lineno}: launches git/devague via {fname}()")
    for m in _GIT_WRITE.finditer(source):
        found.append(f"{name}:{source.count(chr(10), 0, m.start()) + 1}: git write {m.group(0)!r}")
    return found


@pytest.mark.parametrize(
    "snippet",
    [
        "import subprocess\nsubprocess.run(['git', 'checkout', 'main'])\n",
        "import subprocess\nsubprocess.check_output(['git', 'rev-parse', 'HEAD'])\n",
        "from subprocess import Popen\nPopen(['/usr/bin/git', 'status'])\n",
        "import os\nos.system('git checkout -b x')\n",
        "import subprocess\nsubprocess.run(['devague', 'plan', 'waves'])\n",
        "import asyncio\nasyncio.create_subprocess_exec('devague', 'export')\n",
        "import git\n",
        "from pygit2 import Repository\n",
        "CMD = ['git', 'push', 'origin']\n",
    ],
)
def test_scanner_catches_violations(snippet: str) -> None:
    assert scan_source(snippet), snippet


def test_scanner_ignores_prose() -> None:
    ok = 'HELP = "devague records and git remotes are read, never written"\nprint(HELP)\n'
    assert scan_source(ok) == []


def test_library_source_never_runs_git_or_devague() -> None:
    files = sorted(PACKAGE.rglob("*.py"))
    assert any(f.name == "repodocs.py" for f in files)
    violations = [v for f in files for v in scan_source(f.read_text(), str(f))]
    assert violations == []


# --- criterion 4: push docs from >= 5 public repos and read them back -----------------


def test_fixture_sample_round_trip(docs: RepoDocs) -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    assert len({e["repo"] for e in manifest}) >= 5
    ids = {}
    for e in manifest:
        content = (FIXTURES / e["file"]).read_text()
        r = docs.push(
            remote=e["remote"],
            path=e["path"],
            commit=e["commit"],
            committed_at=e["committed_at"],
            default_branch_head=e["commit"],
            visibility=e["visibility"],
            content=content,
        )
        ids[(e["repo"], e["path"])] = (r.doc.id, content)
    for (repo, path), (doc_id, content) in ids.items():
        found = docs.find(
            f"https://github.com/agentculture/{repo}.git", path, include_private=False
        )
        assert found is not None
        assert found.id == doc_id
        assert docs.content(doc_id, include_private=False).decode() == content
    assert len(docs.list(include_private=False)) == len(manifest)


def test_push_rewrites_a_blob_removed_before_its_transaction(docs, store, monkeypatch) -> None:
    """A replace/purge cleanup can unlink the same bytes between push's write_blob and its
    write lock; push must re-write them inside the transaction (PR #16 review)."""
    import hashlib

    sha = hashlib.sha256(b"v-a\n").hexdigest()
    real = store.connect
    fired = []

    def racing_connect():
        if not fired:
            fired.append(True)
            store.blob_path(sha).unlink(missing_ok=True)
        return real()

    monkeypatch.setattr(store, "connect", racing_connect)
    _push(docs)
    assert store.blob_path(sha).read_bytes() == b"v-a\n"
