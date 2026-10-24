"""Migrate items from an xteink data directory (``library.db`` + ``files/<aa>/<sha256>``).

The xteink source is only ever opened read-only (``mode=ro``); nothing is written to it.
Items keep their id, sha256 and created_at. Existing identical items are reported as
unchanged; a clashing id or sha256 is a conflict and never overwritten.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from culture_library.core import Store
from culture_library.core.store import DATA_DIR_ENV, DEFAULT_DATA_DIR  # noqa: F401

NEWSPAPER_PREFIX = "Daily newspaper"


class MigrationError(Exception):
    """Source problem (missing db, missing/corrupt blob)."""


class MigrationConflict(MigrationError):
    """Destination already holds a different item under this id or sha256."""


@dataclass
class Row:
    id: int
    sha256: str
    old_kind: str
    new_kind: str
    title: str
    status: str  # new | unchanged | copied
    sha_ok: bool = False


@dataclass
class Report:
    apply: bool
    visibility: str
    rows: list[Row] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def matched(self) -> int:
        return sum(r.sha_ok for r in self.rows)

    def to_dict(self) -> dict:
        return {
            "apply": self.apply,
            "visibility": self.visibility,
            "total": self.total,
            "matched": self.matched,
            "items": [asdict(r) for r in self.rows],
        }


def map_kind(old_kind: str, title: str) -> str:
    if title.startswith(NEWSPAPER_PREFIX):
        return "newspaper"
    if old_kind == "book":
        return "book"
    return "document"


def _read_source(source: Path) -> list[sqlite3.Row]:
    db = source / "library.db"
    if not db.is_file():
        raise MigrationError(f"no library.db in {source}")
    conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id, sha256, kind, title, author, format, size, created_at"
            " FROM items ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _dest_lookup(dest: Path) -> tuple[dict[int, str], dict[str, int]]:
    """Existing ids/shas in the destination without creating it (read-only)."""
    db = dest / "library.db"
    if not db.is_file():
        return {}, {}
    conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT id, sha256 FROM items").fetchall()
    finally:
        conn.close()
    return {i: s for i, s in rows}, {s: i for i, s in rows}


def migrate_from_xteink(
    source: str | Path,
    dest: str | Path,
    *,
    apply: bool = False,
    visibility: str = "private",
) -> Report:
    source, dest = Path(source), Path(dest)
    items = _read_source(source)
    by_id, by_sha = _dest_lookup(dest)
    report = Report(apply=apply, visibility=visibility)
    plan = []
    for it in items:
        row = Row(
            id=it["id"],
            sha256=it["sha256"],
            old_kind=it["kind"],
            new_kind=map_kind(it["kind"], it["title"]),
            title=it["title"],
            status="new",
        )
        if by_id.get(row.id) == row.sha256:
            row.status = "unchanged"
        elif row.id in by_id or by_sha.get(row.sha256, row.id) != row.id:
            raise MigrationConflict(
                f"item {row.id} ({row.sha256[:12]}) conflicts with an existing destination item"
            )
        report.rows.append(row)
        plan.append((it, row))

    for it, row in plan:
        blob = source / "files" / row.sha256[:2] / row.sha256
        if not blob.is_file():
            if apply or row.status == "new":
                raise MigrationError(f"blob missing for item {row.id}: {blob}")
            continue
        data = blob.read_bytes()
        row.sha_ok = hashlib.sha256(data).hexdigest() == row.sha256
        if not row.sha_ok and row.status == "new":
            raise MigrationError(f"sha256 mismatch for item {row.id}")
    if apply:
        store = Store(dest)
        for it, row in plan:
            if row.status == "unchanged":
                continue
            data = (source / "files" / row.sha256[:2] / row.sha256).read_bytes()
            res = store.add_item(
                data,
                kind=row.new_kind,
                title=row.title,
                format=it["format"],
                author=it["author"],
                visibility=visibility,
                item_id=row.id,
                created_at=it["created_at"],
            )
            if res.item.id != row.id:
                raise MigrationConflict(f"item {row.id} landed as {res.item.id}")
            row.status = "copied"
    return report
