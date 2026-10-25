"""Backups: consistent SQLite snapshot + incremental content-addressed blob copy.

Layout under the backup root::

    <root>/files/<aa>/<sha256>          shared, content-addressed blobs (never rewritten)
    <root>/snapshots/<UTC stamp>/library.db       online-backup copy of the database
    <root>/snapshots/<UTC stamp>/manifest.json    ids, sha256, visibility, counts

The database is copied with SQLite's online backup API, so it is consistent while the
server writes (WAL). Blobs are immutable and named by their hash, so only missing ones
are copied and each copy is verified. Pruning removes old snapshots, never blobs.

Which blobs: every sha256 named by any table with a ``sha256`` column (catalog ``items``,
repo-document ``repodoc_versions``, and any later table), except the ``feed`` log, which
also names blobs of purged items. Backup copies them, restore copies them back, and verify
checks each one. The manifest records items and repo-document versions (with counts); a
snapshot taken before repo documents were covered has no ``repodoc_versions`` list, and
verify reports its repo-document blobs as missing rather than passing it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

BACKUP_DIR_ENV = "CULTURE_LIBRARY_BACKUP_DIR"
DEFAULT_KEEP = 7
SNAPSHOT_ATTEMPTS = 3  # fresh snapshots when a blob is replaced mid-backup (see run_backup)
MANIFEST = "manifest.json"
DB_FILE = "library.db"


class BackupError(Exception):
    """A backup, verify or restore step failed."""


@dataclass
class Snapshot:
    path: Path
    blobs_copied: int
    blobs_total: int
    pruned: list[str] = field(default_factory=list)
    manifest: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "snapshot": str(self.path),
            "items": self.manifest.get("item_count", 0),
            "repodocs": self.manifest.get("repodoc_count", 0),
            "repodoc_versions": self.manifest.get("repodoc_version_count", 0),
            "blobs_copied": self.blobs_copied,
            "blobs_total": self.blobs_total,
            "pruned": self.pruned,
            "visibility_counts": self.manifest.get("visibility_counts", {}),
        }


@dataclass
class Report:
    ok: bool
    items: int
    problems: list[str] = field(default_factory=list)
    target: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "items": self.items,
            "problems": self.problems,
            "target": self.target,
        }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _blob(root: Path, sha: str) -> Path:
    return root / "files" / sha[:2] / sha


def _backup_db(src: Path, dst: Path) -> None:
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def _read_items(db: Path) -> list[dict]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, sha256, visibility, size, kind, title FROM items ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return [r[0] for r in rows]


def _read_repodocs(db: Path) -> tuple[int, list[dict]]:
    """``(doc count, versions)``; ``(0, [])`` for a database without repo documents."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if "repodoc_versions" not in _tables(conn):
            return 0, []
        docs = conn.execute("SELECT count(*) FROM repodocs").fetchone()[0]
        rows = conn.execute(
            "SELECT id, doc_id, sha256, visibility, size FROM repodoc_versions ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return docs, [dict(r) for r in rows]


def _referenced_blobs(db: Path) -> dict[str, str]:
    """``{sha256: table}`` for every blob a table with a ``sha256`` column names (not feed)."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out: dict[str, str] = {}
    try:
        for name in _tables(conn):
            if name == "feed":
                continue
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')}  # nosec B608
            if "sha256" not in cols:
                continue
            sql = f'SELECT DISTINCT sha256 FROM "{name}" WHERE sha256 IS NOT NULL'  # nosec B608
            for (sha,) in conn.execute(sql):
                out.setdefault(sha, name)
    finally:
        conn.close()
    return out


def list_snapshots(root: str | os.PathLike[str]) -> list[Path]:
    snaps = Path(root).expanduser() / "snapshots"
    if not snaps.is_dir():
        return []
    return sorted(p for p in snaps.iterdir() if (p / MANIFEST).is_file())


def _new_snapshot_dir(root: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = root / "snapshots"
    base.mkdir(parents=True, exist_ok=True)
    path, n = base / stamp, 0
    while path.exists():
        n += 1
        path = base / f"{stamp}-{n}"
    path.mkdir()
    return path


def run_backup(
    data_dir: str | os.PathLike[str],
    dest: str | os.PathLike[str],
    *,
    keep: int = DEFAULT_KEEP,
) -> Snapshot:
    """Snapshot ``data_dir`` into ``dest`` and prune to the last ``keep`` snapshots."""
    src = Path(data_dir).expanduser()
    root = Path(dest).expanduser()
    if not (src / DB_FILE).is_file():
        raise BackupError(f"no library.db in {src}")
    if keep < 1:
        raise BackupError("--keep must be at least 1")
    if root.resolve() == src.resolve() or src.resolve() in root.resolve().parents:
        raise BackupError("the backup location must be outside the data directory")
    for attempt in range(1, SNAPSHOT_ATTEMPTS + 1):
        try:
            snap, copied, blobs, manifest = _snapshot_once(src, root)
            break
        except _BlobGone:
            if attempt == SNAPSHOT_ATTEMPTS:
                raise BackupError(
                    f"the library kept changing during {SNAPSHOT_ATTEMPTS} snapshot attempts; "
                    "run the backup again"
                ) from None
    pruned = []
    for old in list_snapshots(root)[:-keep]:
        shutil.rmtree(old)
        pruned.append(old.name)
    return Snapshot(snap, copied, len(blobs), pruned, manifest)


def _snapshot_once(src: Path, root: Path) -> tuple[Path, int, dict[str, str], dict]:
    """One snapshot: copy the database, then every blob it names; discard it on any error.

    The database is copied first, so a replace or purge that commits before a blob is
    copied can unlink a blob the copy still names; that raises :class:`_BlobGone` and
    :func:`run_backup` takes a fresh snapshot.
    """
    snap = _new_snapshot_dir(root)
    try:
        _backup_db(src / DB_FILE, snap / DB_FILE)
        items = _read_items(snap / DB_FILE)
        doc_count, versions = _read_repodocs(snap / DB_FILE)
        blobs = _referenced_blobs(snap / DB_FILE)
        copied = 0
        for sha in sorted(blobs):
            copied += _copy_blob(src, root, sha)
        counts: dict[str, int] = {}
        for it in items:
            counts[it["visibility"]] = counts.get(it["visibility"], 0) + 1
        manifest = {
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "source": str(src),
            "item_count": len(items),
            "visibility_counts": counts,
            "items": [{k: it[k] for k in ("id", "sha256", "visibility", "size")} for it in items],
            "repodoc_count": doc_count,
            "repodoc_version_count": len(versions),
            "repodoc_versions": versions,
        }
        (snap / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
    except BaseException:
        shutil.rmtree(snap, ignore_errors=True)
        raise
    return snap, copied, blobs, manifest


class _BlobGone(BackupError):
    """A blob the snapshot names is gone from the live tree and no live row names it: a
    replace or purge committed after the snapshot was taken."""


def _live_references(src: Path, sha: str) -> bool:
    """Whether the live database still names ``sha`` (read-only, a fresh snapshot)."""
    conn = sqlite3.connect(f"file:{src / DB_FILE}?mode=ro", uri=True)
    try:
        for table in _tables(conn):
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if "sha256" in cols and table != "feed":
                hit = conn.execute(
                    f"SELECT 1 FROM {table} WHERE sha256 = ? LIMIT 1", (sha,)  # nosec B608
                ).fetchone()
                if hit:
                    return True
        return False
    finally:
        conn.close()


def _copy_blob(src: Path, root: Path, sha: str) -> int:
    """Copy one blob if absent in the backup; return 1 if copied. Verifies the copy."""
    out = _blob(root, sha)
    if out.is_file():
        return 0
    inp = _blob(src, sha)
    if not inp.is_file():
        if not _live_references(src, sha):
            raise _BlobGone(f"blob {sha} was replaced or purged after the snapshot")
        raise BackupError(f"blob {sha} is missing from the library files tree")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{sha}.{os.getpid()}.tmp")
    shutil.copyfile(inp, tmp)
    if _sha256_file(tmp) != sha:
        tmp.unlink()
        raise BackupError(f"blob {sha} changed or is corrupt in the library files tree")
    os.replace(tmp, out)
    return 1


def _load_manifest(snapshot: Path) -> dict:
    mf = snapshot / MANIFEST
    if not (snapshot / DB_FILE).is_file() or not mf.is_file():
        raise BackupError(f"{snapshot} is not a snapshot (needs library.db and {MANIFEST})")
    return json.loads(mf.read_text())


def _expected_blobs(db: Path, manifest: dict) -> dict[str, str]:
    """``{sha256: label}``: blobs the manifest declares plus every one the database names."""
    want = {sha: f"{table} row" for sha, table in _referenced_blobs(db).items()}
    for v in manifest.get("repodoc_versions", []):
        want[v["sha256"]] = f"repodoc version {v['id']}"
    for i in manifest["items"]:
        want[i["sha256"]] = f"item {i['id']}"
    return want


def _diff_rows(label: str, wanted: list[dict], present: list[dict]) -> list[str]:
    """Problems where ``wanted`` (manifest) and ``present`` (database) rows disagree by id."""
    problems: list[str] = []
    want = {r["id"]: r for r in wanted}
    got = {r["id"]: r for r in present}
    for rid in sorted(want.keys() | got.keys()):
        w, g = want.get(rid), got.get(rid)
        if w is None or g is None:
            problems.append(f"{label} {rid} present on only one side")
        elif (w["sha256"], w["visibility"]) != (g["sha256"], g["visibility"]):
            problems.append(f"{label} {rid} sha256/visibility differs from manifest")
    return problems


def _check(db: Path, blobs_root: Path, manifest: dict) -> list[str]:
    problems = _diff_rows("item", manifest["items"], _read_items(db))
    if "repodoc_versions" in manifest:
        problems += _diff_rows(
            "repodoc version", manifest["repodoc_versions"], _read_repodocs(db)[1]
        )
    for sha, label in sorted(_expected_blobs(db, manifest).items(), key=lambda kv: kv[1]):
        blob = _blob(blobs_root, sha)
        if not blob.is_file():
            problems.append(f"{label}: blob {sha} missing")
        elif _sha256_file(blob) != sha:
            problems.append(f"{label}: blob {sha} fails sha256 check")
    return problems


def verify(snapshot: str | os.PathLike[str]) -> Report:
    """Check a snapshot's database against its manifest and every referenced blob's sha256."""
    snap = Path(snapshot).expanduser()
    manifest = _load_manifest(snap)
    problems = _check(snap / DB_FILE, snap.parent.parent, manifest)
    return Report(not problems, manifest["item_count"], problems, str(snap))


def restore(snapshot: str | os.PathLike[str], target: str | os.PathLike[str]) -> Report:
    """Restore into an empty/nonexistent ``target`` and verify against the manifest."""
    snap = Path(snapshot).expanduser()
    out = Path(target).expanduser()
    manifest = _load_manifest(snap)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise BackupError(f"restore target {out} is not empty; refusing to overwrite")
    out.mkdir(parents=True, exist_ok=True)
    root = snap.parent.parent
    pre = _check(snap / DB_FILE, root, manifest)
    if pre:
        raise BackupError("snapshot failed verification: " + "; ".join(pre))
    _backup_db(snap / DB_FILE, out / DB_FILE)
    for sha in sorted(_expected_blobs(snap / DB_FILE, manifest)):
        dst = _blob(out, sha)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_blob(root, sha), dst)
    problems = _check(out / DB_FILE, out, manifest)
    return Report(not problems, manifest["item_count"], problems, str(out))


def schedule_text(data_dir: str, dest: str, *, kind: str = "systemd") -> str:
    """Return (never install) a daily schedule: a systemd service+timer pair or a cron line."""
    cmd = f"culture-library backup run --data-dir {data_dir} --to {dest}"
    if kind == "cron":
        return f"17 3 * * * {cmd}\n"
    if kind != "systemd":
        raise BackupError("schedule kind must be systemd or cron")
    return (
        "# culture-library-backup.service\n"
        "[Unit]\nDescription=culture-library backup\n\n"
        f"[Service]\nType=oneshot\nExecStart={cmd}\n\n"
        "# culture-library-backup.timer\n"
        "[Unit]\nDescription=Daily culture-library backup\n\n"
        "[Timer]\nOnCalendar=daily\nRandomizedDelaySec=1h\nPersistent=true\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
