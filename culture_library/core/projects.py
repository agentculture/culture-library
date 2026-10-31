"""Projects (table ``projects``): a repo, the domains it declares, and its connected repos.

One row per repo (normalized with :func:`repodocs.normalize_remote`). ``upsert`` replaces both
lists, so the latest push wins. Stdlib only, no network.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .errors import ValidationError
from .repodocs import normalize_remote
from .store import Store, now, register_schema

_DDL = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL UNIQUE,
    domains TEXT NOT NULL DEFAULT '[]',
    connected TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
)
"""
register_schema(_DDL)


def _str_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ValidationError(f"{field} must be a list of strings")
    return value  # type: ignore[return-value]


GITHUB = "github.com"


def _norm(remote: str) -> str:
    """normalize_remote, also accepting the already-normalized ``host/org/repo`` form.

    A bare ``org/repo`` (two segments, no scheme, no dot in the first segment, as a
    ``culture.yaml`` ``connected`` entry is often written) means ``github.com/org/repo``.
    The doc push itself normalizes its ``remote`` with ``normalize_remote``, which refuses
    that form, so the shorthand never names a pushing repo.
    """
    raw = (remote or "").strip()
    if raw and "://" not in raw and ":" not in raw.split("/", 1)[0] and "/" in raw:
        segs = raw.strip("/").split("/")
        if len(segs) == 2 and "." not in segs[0]:
            raw = f"{GITHUB}/{raw.strip('/')}"
        raw = "https://" + raw
    return normalize_remote(raw)


def clean_connected(connected: list[str]) -> tuple[list[str], list[str]]:
    """Split ``connected`` into the entries that normalize and a warning per dropped one.

    One bad entry in a repo's ``culture.yaml`` must not block that repo's pushes: it is
    left out of the stored list and reported as ``connected 'x' ignored: <reason>``.
    """
    kept: list[str] = []
    warnings: list[str] = []
    for remote in _str_list(connected, "connected"):
        try:
            _norm(remote)
        except ValidationError as exc:
            warnings.append(f"connected {remote!r} ignored: {exc}")
        else:
            kept.append(remote)
    return kept, warnings


def _dedup(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "repo": row["repo"],
        "domains": json.loads(row["domains"]),
        "connected": json.loads(row["connected"]),
        "updated_at": row["updated_at"],
    }


class Projects:
    """Service over a :class:`Store` for project rows."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def upsert(self, repo: str, domains: list[str], connected: list[str]) -> dict[str, Any]:
        """Set (replace) the repo's domains and connected repos; latest push wins."""
        repo_id = _norm(repo)
        doms = _dedup([d.strip() for d in _str_list(domains, "domains") if d.strip()])
        conns = _dedup([_norm(c) for c in _str_list(connected, "connected")])
        with self.store.connect() as conn:
            conn.execute(_DDL)
            conn.execute(
                "INSERT INTO projects (repo, domains, connected, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(repo) DO UPDATE SET domains = excluded.domains, "
                "connected = excluded.connected, updated_at = excluded.updated_at",
                (repo_id, json.dumps(doms), json.dumps(conns), now()),
            )
        return {"repo": repo_id, "domains": doms, "connected": conns}

    def get(self, repo: str) -> dict[str, Any] | None:
        repo_id = _norm(repo)
        with self.store.connect() as conn:
            conn.execute(_DDL)
            row = conn.execute("SELECT * FROM projects WHERE repo = ?", (repo_id,)).fetchone()
        return None if row is None else _row(row)

    def list(self) -> list[dict[str, Any]]:
        with self.store.connect() as conn:
            conn.execute(_DDL)
            rows = conn.execute("SELECT * FROM projects ORDER BY repo").fetchall()
        return [_row(r) for r in rows]

    def repos_for(self, domain: str | None = None) -> list[str]:
        """Sorted repos declaring ``domain`` (all projects when ``None``); unknown gives []."""
        return sorted(p["repo"] for p in self.list() if domain is None or domain in p["domains"])

    def with_connected(self, repo: str) -> list[str]:
        """Sorted: the repo itself plus the repos it declares as connected."""
        repo_id = _norm(repo)
        proj = self.get(repo_id)
        return sorted({repo_id, *(proj["connected"] if proj else [])})


__all__ = ["Projects"]
