"""Backups: consistent SQLite snapshot + incremental content-addressed blob copy.

Layout under the backup root::

    <root>/files/<aa>/<sha256>          shared, content-addressed blobs (never rewritten)
    <root>/snapshots/<UTC stamp>/library.db       online-backup copy of the database
    <root>/snapshots/<UTC stamp>/manifest.json    ids, sha256, visibility, counts

The database is copied with SQLite's online backup API, so it is consistent while the
server writes (WAL). Blobs are immutable and named by their hash, so only missing ones
are copied and each copy is verified. Pruning removes old snapshots, never blobs.
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
MANIFEST = "manifest.json"


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
    if not (src / "library.db").is_file():
        raise BackupError(f"no library.db in {src}")
    if keep < 1:
        raise BackupError("--keep must be at least 1")
    if root.resolve() == src.resolve() or src.resolve() in root.resolve().parents:
        raise BackupError("the backup location must be outside the data directory")
    snap = _new_snapshot_dir(root)
    try:
        _backup_db(src / "library.db", snap / "library.db")
        items = _read_items(snap / "library.db")
        copied = 0
        for it in items:
            copied += _copy_blob(src, root, it["sha256"])
        counts: dict[str, int] = {}
        for it in items:
            counts[it["visibility"]] = counts.get(it["visibility"], 0) + 1
        manifest = {
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "source": str(src),
            "item_count": len(items),
            "visibility_counts": counts,
            "items": [{k: it[k] for k in ("id", "sha256", "visibility", "size")} for it in items],
        }
        (snap / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
    except BaseException:
        shutil.rmtree(snap, ignore_errors=True)
        raise
    pruned = []
    for old in list_snapshots(root)[:-keep]:
        shutil.rmtree(old)
        pruned.append(old.name)
    return Snapshot(snap, copied, len({i["sha256"] for i in items}), pruned, manifest)


def _copy_blob(src: Path, root: Path, sha: str) -> int:
    """Copy one blob if absent in the backup; return 1 if copied. Verifies the copy."""
    out = _blob(root, sha)
    if out.is_file():
        return 0
    inp = _blob(src, sha)
    if not inp.is_file():
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
    if not (snapshot / "library.db").is_file() or not mf.is_file():
        raise BackupError(f"{snapshot} is not a snapshot (needs library.db and {MANIFEST})")
    return json.loads(mf.read_text())


def _check(db: Path, blobs_root: Path, manifest: dict) -> list[str]:
    problems: list[str] = []
    want = {i["id"]: i for i in manifest["items"]}
    got = {i["id"]: i for i in _read_items(db)}
    for iid in sorted(want.keys() | got.keys()):
        w, g = want.get(iid), got.get(iid)
        if w is None or g is None:
            problems.append(f"item {iid} present on only one side")
        elif (w["sha256"], w["visibility"]) != (g["sha256"], g["visibility"]):
            problems.append(f"item {iid} sha256/visibility differs from manifest")
    for iid, w in want.items():
        blob = _blob(blobs_root, w["sha256"])
        if not blob.is_file():
            problems.append(f"item {iid}: blob {w['sha256']} missing")
        elif _sha256_file(blob) != w["sha256"]:
            problems.append(f"item {iid}: blob {w['sha256']} fails sha256 check")
    return problems


def verify(snapshot: str | os.PathLike[str]) -> Report:
    """Check a snapshot's database against its manifest and every blob's sha256."""
    snap = Path(snapshot).expanduser()
    manifest = _load_manifest(snap)
    problems = _check(snap / "library.db", snap.parent.parent, manifest)
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
    pre = _check(snap / "library.db", root, manifest)
    if pre:
        raise BackupError("snapshot failed verification: " + "; ".join(pre))
    _backup_db(snap / "library.db", out / "library.db")
    for it in manifest["items"]:
        dst = _blob(out, it["sha256"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_blob(root, it["sha256"]), dst)
    problems = _check(out / "library.db", out, manifest)
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
