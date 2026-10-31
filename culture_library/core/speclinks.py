"""Spec links: a spec's PR, merge commit and current code paths (plan task t3).

The library never computes links from git (c16). culture-rules sends them: the ``Spec:``
trailer commits when present, else the files of the delivering merge followed through renames
(c53). Links are keyed by the repodocs doc id of the spec. Stdlib only, no network.

The library cannot run git, so "newer" is decided from data the sender gives: each push of
code paths carries ``at_commit`` and ``at_committed_at`` (that commit's ISO-8601 commit time).
A push replaces the stored code paths when its ``at_committed_at`` is the same as or later
than the stored one, so a rename pushed later shows the new path and an older push never
regresses it. At an equal time (two merges in the same second, a force-push) the latest push
wins. :meth:`SpecLinks.record` says whether the push's code paths were kept, so the sender
can tell an older, ignored push from a stored one and resend instead of recording it.

On Postgres (plan library-on-postgres-and-pgvector, task t11) :meth:`SpecLinks.record`
creates the row with ``ON CONFLICT DO NOTHING`` and then holds it ``FOR UPDATE``, so two
concurrent pushes for one spec run one after the other and end as a serial run would. The
lists are ``jsonb`` columns, read back as lists.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .errors import ValidationError
from .store import Store, now

_LINKS_SQL = (
    "SELECT pr_url, pr_number, merge_commit, code_paths_json, at_commit, "
    "at_committed_at, trailer_commits_json FROM spec_links WHERE doc_id = %s"
)


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

    def set_links(self, doc: int, **links: Any) -> dict[str, Any]:
        """Record links for spec ``doc`` and return them (see :meth:`record`)."""
        return self.record(doc, **links)[0]

    def record(
        self,
        doc: int,
        pr: Any = None,
        merge_commit: str | None = None,
        code_paths: list[str] | None = None,
        at_commit: str | None = None,
        trailer_commits: list[str] | None = None,
        at_committed_at: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Record links for spec ``doc``; return ``(links, applied)``.

        ``pr`` and ``merge_commit`` overwrite when given. ``code_paths`` (with ``at_commit``,
        ``at_committed_at`` and ``trailer_commits``) replace the stored set when
        ``at_committed_at`` is the same as or later than the stored one. ``applied`` is false
        only when code paths were sent and kept out because they were older.
        """
        pr_url, pr_number = _pr(pr)
        paths = None if code_paths is None else _str_list(code_paths, "code_paths")
        trailers = _str_list(trailer_commits, "trailer_commits")
        if paths is not None and not (at_commit and at_committed_at):
            raise ValidationError("code_paths needs at_commit and at_committed_at")
        when = _parse_time(at_committed_at) if paths is not None else None
        with self.store.connect() as conn:
            # Create the row if absent, then lock it: a concurrent record waits here until
            # this one commits, so its newer-wins check sees this one's at_committed_at.
            conn.execute(
                "INSERT INTO spec_links (doc_id, updated_at) VALUES (%s, %s)"
                " ON CONFLICT (doc_id) DO NOTHING",
                (doc, now()),
            )
            row = conn.execute(
                "SELECT at_committed_at FROM spec_links WHERE doc_id = %s FOR UPDATE", (doc,)
            ).fetchone()
            if pr_url is not None or pr_number is not None:
                conn.execute(
                    "UPDATE spec_links SET pr_url = %s, pr_number = %s WHERE doc_id = %s",
                    (pr_url, pr_number, doc),
                )
            if merge_commit:
                conn.execute(
                    "UPDATE spec_links SET merge_commit = %s WHERE doc_id = %s",
                    (merge_commit, doc),
                )
            stored = row[0]
            applied = paths is None or stored is None or when >= _parse_time(stored)
            if paths is not None and applied:
                conn.execute(
                    "UPDATE spec_links SET code_paths_json = %s::jsonb, at_commit = %s, "
                    "at_committed_at = %s, trailer_commits_json = %s::jsonb WHERE doc_id = %s",
                    (json.dumps(paths), at_commit, at_committed_at, json.dumps(trailers), doc),
                )
            conn.execute("UPDATE spec_links SET updated_at = %s WHERE doc_id = %s", (now(), doc))
            links = _links(doc, conn.execute(_LINKS_SQL, (doc,)).fetchone())
        return links, applied

    def links(self, doc: int) -> dict[str, Any]:
        """The spec's merge, PR, current code paths and trailer commits (empty if none)."""
        with self.store.reader() as conn:
            row = conn.execute(_LINKS_SQL, (doc,)).fetchone()
        return _links(doc, row)


def _links(doc: int, row: Any) -> dict[str, Any]:
    out = _empty(doc)
    if row is None:
        return out
    if row[0] is not None or row[1] is not None:
        out["pr"] = {"url": row[0], "number": row[1]}
    out.update(
        merge_commit=row[2],
        code_paths=list(row[3]),
        at_commit=row[4],
        at_committed_at=row[5],
        trailer_commits=list(row[6]),
    )
    return out


__all__ = ["SpecLinks"]
