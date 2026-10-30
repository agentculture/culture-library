"""Spec links: a spec's PR, merge commit and current code paths (plan task t3).

The library never computes links from git (c16). culture-rules sends them: the ``Spec:``
trailer commits when present, else the files of the delivering merge followed through renames
(c53). Links are keyed by the repodocs doc id of the spec. Stdlib only, no network.

The library cannot run git, so "newer" is decided from data the sender gives: each push of
code paths carries ``at_commit`` and ``at_committed_at`` (that commit's ISO-8601 commit time).
A push replaces the stored code paths only when its ``at_committed_at`` is strictly later than
the stored one, so a rename pushed later shows the new path and an older (or equal-time,
different-commit) push never regresses it. Re-sending the stored commit is a no-op.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .errors import ValidationError
from .store import Store, now, register_schema

register_schema("""
CREATE TABLE IF NOT EXISTS spec_links (
    doc_id INTEGER PRIMARY KEY,
    pr_url TEXT,
    pr_number INTEGER,
    merge_commit TEXT,
    code_paths_json TEXT NOT NULL DEFAULT '[]',
    at_commit TEXT,
    at_committed_at TEXT,
    trailer_commits_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
)
""")


def _empty(doc: int) -> dict[str, Any]:
    return {
        "doc_id": doc,
        "pr": None,
        "merge_commit": None,
        "code_paths": [],
        "at_commit": None,
        "at_committed_at": None,
        "trailer_commits": [],
    }


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ValidationError(f"at_committed_at is not an ISO-8601 time: {value!r}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _pr(pr: Any) -> tuple[str | None, int | None]:
    if pr is None:
        return None, None
    if isinstance(pr, bool):
        raise ValidationError("pr must be a URL, a number or {url, number}")
    if isinstance(pr, int):
        return None, pr
    if isinstance(pr, str):
        return (pr, None) if pr else (None, None)
    if isinstance(pr, dict):
        number = pr.get("number")
        if number is not None and (isinstance(number, bool) or not isinstance(number, int)):
            raise ValidationError("pr.number must be an integer")
        return pr.get("url") or None, number
    raise ValidationError("pr must be a URL, a number or {url, number}")


def _str_list(values: Any, name: str) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str) or not all(isinstance(v, str) for v in values):
        raise ValidationError(f"{name} must be a list of strings")
    return sorted(set(values))


class SpecLinks:
    """Store and read the links of spec documents."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def set_links(
        self,
        doc: int,
        pr: Any = None,
        merge_commit: str | None = None,
        code_paths: list[str] | None = None,
        at_commit: str | None = None,
        trailer_commits: list[str] | None = None,
        at_committed_at: str | None = None,
    ) -> dict[str, Any]:
        """Record links for spec ``doc`` and return them.

        ``pr`` and ``merge_commit`` overwrite when given. ``code_paths`` (with ``at_commit``,
        ``at_committed_at`` and ``trailer_commits``) replace the stored set only when
        ``at_committed_at`` is later than the stored one.
        """
        pr_url, pr_number = _pr(pr)
        paths = None if code_paths is None else _str_list(code_paths, "code_paths")
        trailers = _str_list(trailer_commits, "trailer_commits")
        if paths is not None and not (at_commit and at_committed_at):
            raise ValidationError("code_paths needs at_commit and at_committed_at")
        when = _parse_time(at_committed_at) if paths is not None else None
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT at_committed_at FROM spec_links WHERE doc_id = ?", (doc,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO spec_links (doc_id, updated_at) VALUES (?, ?)", (doc, now())
                )
            if pr_url is not None or pr_number is not None:
                conn.execute(
                    "UPDATE spec_links SET pr_url = ?, pr_number = ? WHERE doc_id = ?",
                    (pr_url, pr_number, doc),
                )
            if merge_commit:
                conn.execute(
                    "UPDATE spec_links SET merge_commit = ? WHERE doc_id = ?", (merge_commit, doc)
                )
            stored = row[0] if row else None
            if paths is not None and (stored is None or when > _parse_time(stored)):
                conn.execute(
                    "UPDATE spec_links SET code_paths_json = ?, at_commit = ?, "
                    "at_committed_at = ?, trailer_commits_json = ? WHERE doc_id = ?",
                    (json.dumps(paths), at_commit, at_committed_at, json.dumps(trailers), doc),
                )
            conn.execute("UPDATE spec_links SET updated_at = ? WHERE doc_id = ?", (now(), doc))
        return self.links(doc)

    def links(self, doc: int) -> dict[str, Any]:
        """The spec's merge, PR, current code paths and trailer commits (empty if none)."""
        with self.store.reader() as conn:
            row = conn.execute(
                "SELECT pr_url, pr_number, merge_commit, code_paths_json, at_commit, "
                "at_committed_at, trailer_commits_json FROM spec_links WHERE doc_id = ?",
                (doc,),
            ).fetchone()
        out = _empty(doc)
        if row is None:
            return out
        if row[0] is not None or row[1] is not None:
            out["pr"] = {"url": row[0], "number": row[1]}
        out.update(
            merge_commit=row[2],
            code_paths=json.loads(row[3]),
            at_commit=row[4],
            at_committed_at=row[5],
            trailer_commits=json.loads(row[6]),
        )
        return out


__all__ = ["SpecLinks"]
