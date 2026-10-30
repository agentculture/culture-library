"""Tests for scripts/verify-mirror.py: offline, with a fake library and fake S3."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from culture_library.core.mirror import key_for

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify-mirror.py"
_spec = importlib.util.spec_from_file_location("verify_mirror", SCRIPT)
vm = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["verify_mirror"] = vm
_spec.loader.exec_module(vm)

REMOTE = "https://github.com/acme/alpha.git"
REPO = "github.com/acme/alpha"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def make_repo(root: Path, name: str, remote: str | None = REMOTE) -> Path:
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    cfg = "[core]\n\tbare = false\n"
    if remote:
        cfg += (
            f'[remote "origin"]\n\turl = {remote}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
        )
    (repo / ".git" / "config").write_text(cfg)
    return repo


def write(repo: Path, rel: str, data: bytes) -> bytes:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return data


class FakeLibrary:
    def __init__(self) -> None:
        self.docs: dict[tuple[str, str], vm.LibDoc] = {}

    def put(self, repo, path, data, *, commit="c1", draft=None):
        shas = {sha(data)} | ({sha(draft)} if draft is not None else set())
        self.docs[(repo, path)] = vm.LibDoc(shas=shas, commits={commit: sha(data)})

    def lookup(self, repo, path):
        return self.docs.get((repo, path))


class FakeS3:
    def __init__(self) -> None:
        self.shas: dict[str, str] = {}

    def put(self, key, data):
        self.shas[key] = sha(data)

    def head(self, key):
        return self.shas.get(key)


def test_remote_parsed_from_git_config_without_git(tmp_path):
    repo = make_repo(tmp_path, "alpha")
    assert vm.repo_identity(repo) == REPO


def test_remoteless_falls_back_to_host_path(tmp_path):
    repo = make_repo(tmp_path, "solo", remote=None)
    ident = vm.repo_identity(repo)
    assert ident.endswith(f":{repo.resolve()}") and ":/" in ident


def test_walk_covers_pushed_prefixes_only_and_skips_worktrees(tmp_path):
    repo = make_repo(tmp_path, "alpha")
    write(repo, "docs/specs/a.md", b"a")
    write(repo, "docs/design/img.png", b"p")
    write(repo, ".devague/x/y.json", b"{}")
    write(repo, "docs/other/skip.md", b"s")
    write(repo, "src/skip.py", b"s")
    write(repo, ".worktrees.alpha/t1/docs/specs/w.md", b"w")
    write(repo, "feature-worktrees/t1/docs/plans/w.md", b"w")
    got = [p for p, _ in vm.iter_docs(repo)]
    assert sorted(got) == [".devague/x/y.json", "docs/design/img.png", "docs/specs/a.md"]


def test_discover_all_skips_worktree_dirs(tmp_path):
    make_repo(tmp_path, "alpha")
    make_repo(tmp_path, ".worktrees.alpha")
    make_repo(tmp_path, "alpha-worktrees")
    (tmp_path / "notrepo").mkdir()
    assert [r.name for r in vm.discover_repos(tmp_path)] == ["alpha"]


def build(tmp_path, *, lib_ok=True, s3_ok=True):
    repo = make_repo(tmp_path, "alpha")
    lib, s3 = FakeLibrary(), FakeS3()
    a = write(repo, "docs/specs/a.md", b"spec a")
    b = write(repo, "docs/plans/b.md", b"plan b")
    lib.put(REPO, "docs/specs/a.md", a)
    s3.put(key_for(REPO, "docs/specs/a.md", "c1"), a)
    if lib_ok:
        lib.put(REPO, "docs/plans/b.md", b, draft=None)
    if s3_ok:
        s3.put(key_for(REPO, "docs/plans/b.md", "c1"), b)
    return lib, s3


def test_all_ok_exits_zero(tmp_path, capsys):
    lib, s3 = build(tmp_path)
    rc = vm.main(["--repos", "alpha", "--root", str(tmp_path)], library=lib, s3=s3)
    assert rc == 0
    assert "ok" in capsys.readouterr().out


def test_missing_in_library_and_s3_exit_nonzero_with_counts(tmp_path, capsys):
    lib, s3 = build(tmp_path, lib_ok=False, s3_ok=False)
    rc = vm.main(["--repos", "alpha", "--root", str(tmp_path), "--json"], library=lib, s3=s3)
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["totals"]["library"] == {"ok": 1, "missing": 1, "mismatched": 0}
    assert out["totals"]["s3"]["missing"] == 1
    assert out["repos"]["alpha"]["library"]["ok"] == 1


def test_mismatch_detected(tmp_path, capsys):
    lib, s3 = build(tmp_path)
    s3.put(key_for(REPO, "docs/specs/a.md", "c1"), b"different")
    lib.put(REPO, "docs/plans/b.md", b"stale")
    rc = vm.main(["--repos", "alpha", "--root", str(tmp_path), "--json"], library=lib, s3=s3)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["totals"]["library"]["mismatched"] == 1
    assert out["totals"]["s3"]["mismatched"] == 1


def test_draft_sha_matches_library(tmp_path, capsys):
    repo = make_repo(tmp_path, "alpha")
    lib, s3 = FakeLibrary(), FakeS3()
    new = write(repo, "docs/specs/a.md", b"edited")
    lib.put(REPO, "docs/specs/a.md", b"committed", draft=new)
    s3.put(key_for(REPO, "docs/specs/a.md", "working"), new)
    rc = vm.main(["--repos", "alpha", "--root", str(tmp_path)], library=lib, s3=s3)
    assert rc == 0


def test_no_s3_skips_s3_check(tmp_path, capsys):
    lib, s3 = build(tmp_path, s3_ok=False)
    rc = vm.main(["--repos", "alpha", "--root", str(tmp_path), "--no-s3", "--json"], library=lib)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["totals"]["s3"] is None


def test_all_flag_and_unknown_repo(tmp_path, capsys):
    lib, s3 = build(tmp_path)
    make_repo(tmp_path, "alpha-worktrees")
    assert vm.main(["--all", "--root", str(tmp_path)], library=lib, s3=s3) == 0
    assert vm.main(["--repos", "nope", "--root", str(tmp_path)], library=lib, s3=s3) == 2


def test_requires_repos_or_all(tmp_path):
    with pytest.raises(SystemExit):
        vm.main(["--root", str(tmp_path)], library=FakeLibrary(), s3=FakeS3())


def test_http_library_parses_api(monkeypatch):
    calls = []

    def fake_get(url, key):
        calls.append(url)
        if "/api/repodocs?" in url:
            return {"docs": [{"id": 7, "repo": REPO, "path": "docs/specs/a.md"}]}
        return {
            "doc": {"id": 7, "current_commit": "c1"},
            "versions": [{"commit": "c1", "sha256": "aa"}],
            "draft": {"sha256": "bb"},
        }

    client = vm.HttpLibrary("http://x", "k", getter=fake_get)
    doc = client.lookup(REPO, "docs/specs/a.md")
    assert doc.shas == {"aa", "bb"} and doc.commits == {"c1": "aa"}
    assert "remote=" in calls[0]
