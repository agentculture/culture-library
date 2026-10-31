#!/usr/bin/env python3
"""Verify the repo-document mirror: files on disk vs the library API and S3.

For each repo, every file under the pushed prefixes (docs/specs/, docs/plans/,
docs/deliveries/, docs/design/ and everything under .devague/) is hashed (sha256) and
compared with:

* the library (``GET /api/repodocs``): ok when the file's sha256 equals the doc's current
  committed version OR its working draft;
* S3: ok when the object at ``key_for(repo, path, commit-or-working)`` carries the same sha256
  in its ``sha256`` metadata.

Usage::

    verify-mirror.py --repos culture-library,steward [--root ~/git] [--no-s3] [--json]
    verify-mirror.py --all

Env: CULTURE_LIBRARY_API_KEY, CULTURE_LIBRARY_URL (default http://127.0.0.1:8796);
for S3 (needs boto3): CULTURE_LIBRARY_S3_BUCKET and optionally CULTURE_LIBRARY_S3_ENDPOINT.
Exit 0 when everything is ok, 1 on any missing or mismatched file, 2 on a usage or
environment error. It reads the filesystem only; it never runs git.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import pathlib
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from culture_library.core.mirror import WORKING, key_for  # noqa: E402
from culture_library.core.errors import ValidationError  # noqa: E402
from culture_library.core.repodocs import normalize_remote  # noqa: E402

PREFIXES = ("docs/specs/", "docs/plans/", "docs/deliveries/", "docs/design/", ".devague/")
SKIP_DIRS = (".worktrees.*", "*-worktrees", ".git", "node_modules", ".venv", "__pycache__")
DEFAULT_URL = "http://127.0.0.1:8796"
_ORIGIN_RE = re.compile(r'^\[remote "origin"\]\s*$')


class VerifyError(Exception):
    pass


@dataclass
class LibDoc:
    shas: set[str] = field(default_factory=set)  # current version sha + draft sha
    commits: dict[str, str] = field(default_factory=dict)  # commit -> sha256


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _skipped(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in SKIP_DIRS)


def _git_config(repo: pathlib.Path) -> pathlib.Path | None:
    dotgit = repo / ".git"
    if dotgit.is_dir():
        return dotgit / "config"
    if dotgit.is_file():  # worktree/submodule: "gitdir: <path>"
        text = dotgit.read_text(errors="replace").strip()
        if text.startswith("gitdir:"):
            gitdir = pathlib.Path(text[7:].strip())
            if not gitdir.is_absolute():
                gitdir = repo / gitdir
            common = gitdir / "commondir"
            if common.is_file():
                gitdir = (gitdir / common.read_text().strip()).resolve()
            return gitdir / "config"
    return None


def origin_url(repo: pathlib.Path) -> str | None:
    cfg = _git_config(repo)
    if cfg is None or not cfg.is_file():
        return None
    in_origin = False
    for line in cfg.read_text(errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_origin = bool(_ORIGIN_RE.match(stripped))
        elif in_origin and stripped.startswith("url") and "=" in stripped:
            return stripped.split("=", 1)[1].strip()
    return None


DEFAULT_HOST = "spark"
# origins culture-rules treats as no origin (a path on this host)
_LOCAL_ORIGINS = (
    "/",
    "./",
    "../",
    "~",
    "file:",
)  # the node culture-rules' repodocs-sync rule is placed on


def repo_identity(repo: pathlib.Path, host: str = DEFAULT_HOST) -> str:
    """Normalized remote, or ``<host>:<abs path>`` for a repo with no origin. ``host`` is the
    pusher's node name (culture-rules uses its own, not this machine's hostname)."""
    url = origin_url(repo)
    if url and not url.lower().startswith(_LOCAL_ORIGINS):
        return normalize_remote(url)
    return normalize_remote(f"{host}:{repo.resolve()}")


def iter_docs(repo: pathlib.Path):
    """Yield ``(posix relative path, absolute path)`` for files under the pushed prefixes."""
    for dirpath, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if not _skipped(d))
        base = pathlib.Path(dirpath)
        for name in sorted(files):
            full = base / name
            rel = full.relative_to(repo).as_posix()
            if rel.startswith(PREFIXES):
                yield rel, full


def _name_key(name: str) -> str:
    return name.lower().replace("_", "-")


def _owners(repos: list[pathlib.Path]) -> list[pathlib.Path]:
    """One clone per shared origin, as culture-rules picks it: the clone named like the
    origin's repo (case-insensitive, ``-`` and ``_`` equal), else the first by name. Only that
    clone's drafts are pushed, so the others are not checked."""
    groups: dict[str, list[pathlib.Path]] = {}
    for repo in repos:
        url = origin_url(repo)
        try:
            shared = url and not url.lower().startswith(_LOCAL_ORIGINS)
            key = normalize_remote(url).lower() if shared else str(repo)
        except ValidationError:
            key = str(repo)
        groups.setdefault(key, []).append(repo)
    kept = []
    for key, clones in groups.items():
        want = _name_key(key.rsplit("/", 1)[-1])
        named = [c for c in clones if _name_key(c.name) == want]
        kept.append(min(named or clones, key=lambda c: c.name))
    return sorted(kept, key=lambda c: c.name)


def discover_repos(root: pathlib.Path) -> list[pathlib.Path]:
    return _owners(
        [
            p
            for p in sorted(root.iterdir())
            # a real .git directory only: culture-rules skips linked worktrees (.git is a file)
            if p.is_dir() and not _skipped(p.name) and (p / ".git").is_dir()
        ]
    )


class HttpLibrary:
    """Library API client; ``getter(url, key) -> dict`` is injectable."""

    def __init__(self, base: str, key: str, getter=None) -> None:
        self.base, self.key = base.rstrip("/"), key
        self._get = getter or self._http_get

    @staticmethod
    def _http_get(url: str, key: str) -> dict:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - operator URL
            return json.loads(resp.read())

    def lookup(self, repo: str, path: str) -> LibDoc | None:
        # send a URL form: a library before 0.19.1 refuses the bare repo id as a filter
        remote = repo if ":" in repo else f"https://{repo}"
        q = urllib.parse.urlencode({"remote": remote, "path": path})
        found = self._get(f"{self.base}/api/repodocs?{q}", self.key).get("docs", [])
        found = [d for d in found if d.get("repo") == repo and d.get("path") == path]
        if not found:
            return None
        detail = self._get(f"{self.base}/api/repodocs/{found[0]['id']}", self.key)
        doc = LibDoc()
        current = (detail.get("doc") or {}).get("current_commit")
        for v in detail.get("versions") or []:
            doc.commits[v["commit"]] = v["sha256"]
            if v["commit"] == current:
                doc.shas.add(v["sha256"])
        draft = detail.get("draft") or detail.get("working")
        if draft and draft.get("sha256"):
            doc.shas.add(draft["sha256"])
        return doc


class Boto3S3:
    """S3 head via boto3; reads the sha256 from object metadata."""

    def __init__(self, bucket: str, endpoint: str | None = None) -> None:
        import boto3  # noqa: PLC0415 - optional

        self.bucket = bucket
        self.client = boto3.client("s3", endpoint_url=endpoint) if endpoint else boto3.client("s3")

    def head(self, key: str) -> str | None:
        try:
            meta = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return (meta.get("Metadata") or {}).get("sha256") or ""


def _new() -> dict[str, int]:
    return {"ok": 0, "missing": 0, "mismatched": 0}


def check_library(lib, repo_id: str, rel: str, sha: str) -> tuple[str, LibDoc | None]:
    doc = lib.lookup(repo_id, rel)
    if doc is None:
        return "missing", None
    return ("ok" if sha in doc.shas else "mismatched"), doc


def check_s3(s3, repo_id: str, rel: str, sha: str, doc: LibDoc | None) -> str:
    commits = [c for c, s in (doc.commits.items() if doc else ()) if s == sha]
    keys = [key_for(repo_id, rel, WORKING)] + [key_for(repo_id, rel, c) for c in commits]
    if doc:  # also probe the other versions so a stale object reads as mismatched
        keys += [key_for(repo_id, rel, c) for c in doc.commits if c not in commits]
    seen_any = False
    for key in keys:
        got = s3.head(key)
        if got is None:
            continue
        seen_any = True
        if got == sha:
            return "ok"
    return "mismatched" if seen_any else "missing"


def verify(repos, lib, s3, host: str = DEFAULT_HOST) -> dict:
    result = {"repos": {}, "totals": {"library": _new(), "s3": _new() if s3 else None}}
    problems: list[str] = []
    for repo in repos:
        rid = repo_identity(repo, host)
        counts = {"id": rid, "files": 0, "library": _new(), "s3": _new() if s3 else None}
        for rel, full in iter_docs(repo):
            sha = sha256_file(full)
            counts["files"] += 1
            status, doc = check_library(lib, rid, rel, sha)
            counts["library"][status] += 1
            result["totals"]["library"][status] += 1
            if status != "ok":
                problems.append(f"library {status}: {repo.name}/{rel}")
            if s3:
                st = check_s3(s3, rid, rel, sha, doc)
                counts["s3"][st] += 1
                result["totals"]["s3"][st] += 1
                if st != "ok":
                    problems.append(f"s3 {st}: {repo.name}/{rel}")
        result["repos"][repo.name] = counts
    result["problems"] = problems
    return result


def _line(label: str, c: dict | None) -> str:
    if c is None:
        return f"{label}: skipped"
    return f"{label}: {c['ok']} ok, {c['missing']} missing, {c['mismatched']} mismatched"


def render(result: dict) -> str:
    out = []
    for name, c in result["repos"].items():
        out.append(f"{name} ({c['id']}, {c['files']} files)")
        out.append("  " + _line("library", c["library"]))
        out.append("  " + _line("s3", c["s3"]))
    out += result["problems"]
    out.append("TOTAL " + _line("library", result["totals"]["library"]))
    out.append("TOTAL " + _line("s3", result["totals"]["s3"]))
    return "\n".join(out)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--repos", help="comma-separated repo names (under --root) or paths")
    p.add_argument("--all", action="store_true", help="every git repo directly under --root")
    p.add_argument("--root", default=str(pathlib.Path.home() / "git"))
    p.add_argument("--no-s3", action="store_true", help="skip the S3 check")
    p.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"pusher's node name for remoteless repos (default {DEFAULT_HOST})",
    )
    p.add_argument("--json", action="store_true")
    return p


def main(argv=None, library=None, s3=None) -> int:
    args = _parser().parse_args(argv)
    if not args.repos and not args.all:
        _parser().error("pass --repos A,B,... or --all")
    root = pathlib.Path(args.root).expanduser()
    try:
        if args.all:
            repos = discover_repos(root)
        else:
            repos = []
            for name in filter(None, (n.strip() for n in args.repos.split(","))):
                path = pathlib.Path(name).expanduser()
                path = path if path.is_absolute() else root / name
                if not (path / ".git").exists():
                    raise VerifyError(f"not a git repo: {path}")
                repos.append(path)
        if library is None:
            key = os.environ.get("CULTURE_LIBRARY_API_KEY")
            if not key:
                raise VerifyError("CULTURE_LIBRARY_API_KEY is not set")
            library = HttpLibrary(os.environ.get("CULTURE_LIBRARY_URL", DEFAULT_URL), key)
        if s3 is None and not args.no_s3:
            bucket = os.environ.get("CULTURE_LIBRARY_S3_BUCKET")
            if not bucket:
                raise VerifyError("CULTURE_LIBRARY_S3_BUCKET is not set (or pass --no-s3)")
            try:
                s3 = Boto3S3(bucket, os.environ.get("CULTURE_LIBRARY_S3_ENDPOINT"))
            except ImportError as exc:
                raise VerifyError("boto3 is not installed (or pass --no-s3)") from exc
        if args.no_s3:
            s3 = None
        result = verify(repos, library, s3, args.host)
    except (VerifyError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2) if args.json else render(result))
    bad = sum(
        c[k]
        for t in ("library", "s3")
        if result["totals"][t]
        for c in [result["totals"][t]]
        for k in ("missing", "mismatched")
    )
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
