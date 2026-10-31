"""Backups: a consistent ``pg_dump`` of the Postgres database + incremental blob copy.

Layout under the backup root::

    <root>/files/<aa>/<sha256>               shared, content-addressed blobs (never rewritten)
    <root>/snapshots/<UTC stamp>/library.dump    pg_dump custom-format dump of the database
    <root>/snapshots/<UTC stamp>/manifest.json   items, repo-document versions, per-table
                                                 row counts, blob list, dump sha256

The dump and the manifest are read from one exported transaction snapshot
(``pg_export_snapshot`` + ``pg_dump --snapshot``), so counts and ids in the manifest are
exactly what the dump holds while the server keeps writing. Blobs are immutable and named by
their hash, so only missing ones are copied and each copy is verified. Pruning removes old
snapshots, never blobs.

Which blobs: every sha256 named by any public table with a ``sha256`` column (catalog
``items``, repo-document ``repodoc_versions``, drafts, the mirror outbox, and any later
table), except the ``feed`` log, which also names blobs of purged items.

``verify`` needs no database: it checks the dump file against the manifest's sha256, lists
the dump with ``pg_restore --list`` to confirm every manifest table is in it, and re-hashes
every blob the manifest names. ``restore`` loads the dump into an *empty* database with
``pg_restore``, then compares the restored per-table row counts, items and repo-document
versions with the manifest and copies the blobs back, re-hashing each.

The client tools come from ``CULTURE_LIBRARY_PG_BIN_DIR`` (a directory holding ``pg_dump`` and
``pg_restore``) or, when that is unset, from ``PATH``; a missing tool raises an error naming it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess  # nosec B404 - runs pg_dump / pg_restore with an argv list, never a shell
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import CoreError
from .store import _psycopg, conninfo_from_env

BACKUP_DIR_ENV = "CULTURE_LIBRARY_BACKUP_DIR"
PG_BIN_DIR_ENV = "CULTURE_LIBRARY_PG_BIN_DIR"
DEFAULT_KEEP = 7
SNAPSHOT_ATTEMPTS = 3  # fresh snapshots when a blob is replaced mid-backup (see run_backup)
MANIFEST = "manifest.json"
DUMP_FILE = "library.dump"
FEED_TABLE = "feed"


class BackupError(CoreError):
    """A backup, verify or restore step failed."""


class PgToolMissing(BackupError):
    """pg_dump / pg_restore cannot be found (an environment problem, not bad input)."""


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
            "tables": self.manifest.get("tables", {}),
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


# ------------------------------------------------------------------ pg client tools


def _tool(name: str) -> str:
    """Path of ``pg_dump`` / ``pg_restore``: ``CULTURE_LIBRARY_PG_BIN_DIR`` first, then PATH."""
    bin_dir = os.environ.get(PG_BIN_DIR_ENV)
    if bin_dir:
        cand = Path(bin_dir).expanduser() / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
        raise PgToolMissing(f"{name} not found in {PG_BIN_DIR_ENV}={bin_dir}")
    found = shutil.which(name)
    if found:
        return found
    raise PgToolMissing(
        f"{name} not found on PATH; install the PostgreSQL 17 client tools or set "
        f"{PG_BIN_DIR_ENV} to the directory that holds pg_dump and pg_restore"
    )


def _conn_args(conninfo: str) -> tuple[str, dict[str, str]]:
    """``(--dbname value without the password, env)``: the password travels as PGPASSWORD."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    parts = conninfo_to_dict(conninfo)
    env = dict(os.environ)
    password = parts.pop("password", None)
    if password:
        env["PGPASSWORD"] = str(password)
    return make_conninfo("", **parts), env


def _run_tool(argv: list[str], env: dict[str, str], *, stdin=None, stdout=None) -> bytes:
    try:
        proc = subprocess.run(  # nosec B603 - argv list, tool path resolved by _tool
            argv, env=env, stdin=stdin, stdout=stdout or subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )  # fmt: skip
    except OSError as exc:
        raise PgToolMissing(f"cannot run {argv[0]}: {exc}; check {PG_BIN_DIR_ENV}") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        raise BackupError(f"{Path(argv[0]).name} failed ({proc.returncode}): {err}")
    return proc.stdout or b""


def _pg_dump(conninfo: str, snapshot_id: str, dst: Path) -> None:
    """Dump the database, as of the exported snapshot, to ``dst`` (custom format)."""
    dbname, env = _conn_args(conninfo)
    argv = [_tool("pg_dump"), "--format=custom", "--no-owner", f"--snapshot={snapshot_id}"]
    argv.append(f"--dbname={dbname}")
    with dst.open("wb") as fh:
        _run_tool(argv, env, stdout=fh)


def _pg_restore(conninfo: str, dump: Path) -> None:
    dbname, env = _conn_args(conninfo)
    argv = [_tool("pg_restore"), "--no-owner", "--exit-on-error", "--single-transaction"]
    argv.append(f"--dbname={dbname}")
    with dump.open("rb") as fh:
        _run_tool(argv, env, stdin=fh)


def _dump_tables(dump: Path) -> set[str]:
    """Tables whose data the dump holds, from ``pg_restore --list``."""
    argv = [_tool("pg_restore"), "--list"]
    with dump.open("rb") as fh:
        out = _run_tool(argv, dict(os.environ), stdin=fh).decode(errors="replace")
    tables = set()
    for line in out.splitlines():
        if line.startswith(";"):
            continue
        parts = line.split(" TABLE DATA ", 1)
        if len(parts) == 2:
            fields = parts[1].split()  # schema, table, owner
            if len(fields) >= 2 and fields[0] == "public":
                tables.add(fields[1])
    return tables


# ------------------------------------------------------------------ reading the database


def _table_names(conn: Any) -> list[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name"
    ).fetchall()
    return [r[0] for r in rows]


def _table_counts(conn: Any, tables: list[str] | None = None) -> dict[str, int]:
    from psycopg import sql

    out = {}
    for name in tables if tables is not None else _table_names(conn):
        query = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(name))
        out[name] = conn.execute(query).fetchone()[0]
    return out


def _read_items(conn: Any, tables: list[str]) -> list[dict]:
    if "items" not in tables:
        return []
    cur = conn.execute("SELECT id, sha256, visibility, size, kind, title FROM items ORDER BY id")
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _read_versions(conn: Any, tables: list[str]) -> list[dict]:
    if "repodoc_versions" not in tables:
        return []
    cur = conn.execute(
        "SELECT id, doc_id, sha256, visibility, size FROM repodoc_versions ORDER BY id"
    )
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _referenced_blobs(conn: Any) -> dict[str, str]:
    """``{sha256: table}`` for every blob a public table with a ``sha256`` column names."""
    from psycopg import sql

    rows = conn.execute(
        "SELECT table_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name = 'sha256' ORDER BY table_name"
    ).fetchall()
    out: dict[str, str] = {}
    for (name,) in rows:
        if name == FEED_TABLE:
            continue
        query = sql.SQL("SELECT DISTINCT sha256 FROM {} WHERE sha256 IS NOT NULL").format(
            sql.Identifier(name)
        )
        for (sha,) in conn.execute(query):
            out.setdefault(sha, name)
    return out


def _live_blobs(conninfo: str) -> dict[str, str]:
    with _psycopg().connect(conninfo) as conn:
        return _referenced_blobs(conn)


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
    dsn: str | None = None,
) -> Snapshot:
    """Snapshot the database and ``data_dir``'s files into ``dest``; keep the last ``keep``.

    ``dsn`` defaults to the ``CULTURE_LIBRARY_PG_*`` settings the Store reads.
    """
    src = Path(data_dir).expanduser()
    root = Path(dest).expanduser()
    if not (src / "files").is_dir():
        raise BackupError(f"no files tree in {src}")
    if keep < 1:
        raise BackupError("--keep must be at least 1")
    if root.resolve() == src.resolve() or src.resolve() in root.resolve().parents:
        raise BackupError("the backup location must be outside the data directory")
    conninfo = dsn if dsn is not None else conninfo_from_env()
    for attempt in range(1, SNAPSHOT_ATTEMPTS + 1):
        try:
            snap, copied, blobs, manifest = _snapshot_once(src, root, conninfo)
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


def _read_state(conninfo: str, dump: Path) -> tuple[dict, dict[str, str]]:
    """Dump the database and read the manifest facts from one exported transaction snapshot."""
    psycopg = _psycopg()
    try:
        conn = psycopg.connect(conninfo, connect_timeout=10)
    except psycopg.OperationalError as exc:
        raise BackupError(f"cannot connect to the library database: {exc}") from exc
    with conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        snapshot_id = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
        _pg_dump(conninfo, snapshot_id, dump)  # the exporting transaction is still open
        tables = _table_names(conn)
        counts = _table_counts(conn, tables)
        items = _read_items(conn, tables)
        versions = _read_versions(conn, tables)
        blobs = _referenced_blobs(conn)
        version = conn.execute("SELECT version()").fetchone()[0]
    visibility: dict[str, int] = {}
    for it in items:
        visibility[it["visibility"]] = visibility.get(it["visibility"], 0) + 1
    facts = {
        "postgres": version,
        "item_count": len(items),
        "visibility_counts": visibility,
        "items": [{k: it[k] for k in ("id", "sha256", "visibility", "size")} for it in items],
        "repodoc_count": counts.get("repodocs", 0),
        "repodoc_version_count": len(versions),
        "repodoc_versions": versions,
        "tables": counts,
    }
    return facts, blobs


def _snapshot_once(src: Path, root: Path, conninfo: str) -> tuple[Path, int, dict[str, str], dict]:
    """One snapshot: dump the database, then copy every blob it names; discard on any error.

    The dump is taken first, so a replace or purge that commits before a blob is copied can
    unlink a blob the dump still names; that raises :class:`_BlobGone` and :func:`run_backup`
    takes a fresh snapshot.
    """
    snap = _new_snapshot_dir(root)
    try:
        facts, blobs = _read_state(conninfo, snap / DUMP_FILE)
        copied = 0
        for sha in sorted(blobs):
            copied += _copy_blob(src, root, sha, conninfo)
        manifest = {
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "source": str(src),
            "dump": DUMP_FILE,
            "dump_sha256": _sha256_file(snap / DUMP_FILE),
            "dump_size": (snap / DUMP_FILE).stat().st_size,
            **facts,
            "blobs": blobs,
        }
        (snap / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
    except BaseException:
        shutil.rmtree(snap, ignore_errors=True)
        raise
    return snap, copied, blobs, manifest


class _BlobGone(BackupError):
    """A blob the snapshot names is gone from the live tree and no live row names it: a
    replace or purge committed after the snapshot was taken."""


def _copy_blob(src: Path, root: Path, sha: str, conninfo: str) -> int:
    """Copy one blob if absent in the backup; return 1 if copied. Verifies the copy."""
    out = _blob(root, sha)
    if out.is_file():
        return 0
    inp = _blob(src, sha)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{sha}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(inp, tmp)  # a missing file, before or during the copy, lands here
    except FileNotFoundError:
        tmp.unlink(missing_ok=True)
        if sha not in _live_blobs(conninfo):  # the live database, a fresh connection
            raise _BlobGone(f"blob {sha} was replaced or purged after the snapshot") from None
        raise BackupError(f"blob {sha} is missing from the library files tree") from None
    if _sha256_file(tmp) != sha:
        tmp.unlink()
        raise BackupError(f"blob {sha} changed or is corrupt in the library files tree")
    os.replace(tmp, out)
    return 1


def _load_manifest(snapshot: Path) -> dict:
    mf = snapshot / MANIFEST
    if not (snapshot / DUMP_FILE).is_file() or not mf.is_file():
        raise BackupError(f"{snapshot} is not a snapshot (needs {DUMP_FILE} and {MANIFEST})")
    return json.loads(mf.read_text())


def _expected_blobs(manifest: dict) -> dict[str, str]:
    """``{sha256: label}`` for every blob the manifest names."""
    want = {sha: f"{table} row" for sha, table in manifest.get("blobs", {}).items()}
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


def _check_blobs(blobs_root: Path, manifest: dict) -> list[str]:
    problems = []
    for sha, label in sorted(_expected_blobs(manifest).items(), key=lambda kv: kv[1]):
        blob = _blob(blobs_root, sha)
        if not blob.is_file():
            problems.append(f"{label}: blob {sha} missing")
        elif _sha256_file(blob) != sha:
            problems.append(f"{label}: blob {sha} fails sha256 check")
    return problems


def _check_dump(snapshot: Path, manifest: dict) -> list[str]:
    """The dump file is the one the manifest describes and holds every manifest table."""
    dump = snapshot / DUMP_FILE
    problems = []
    if _sha256_file(dump) != manifest.get("dump_sha256"):
        problems.append(f"{DUMP_FILE} fails its sha256 check against the manifest")
        return problems
    missing = sorted(set(manifest.get("tables", {})) - _dump_tables(dump))
    problems += [f"table {name} is in the manifest but not in {DUMP_FILE}" for name in missing]
    return problems


def _check_database(conninfo: str, manifest: dict) -> list[str]:
    """Compare a restored database with the manifest: row counts, items, versions."""
    with _psycopg().connect(conninfo) as conn:
        tables = _table_names(conn)
        counts = _table_counts(conn, [t for t in manifest["tables"] if t in tables])
        items = _read_items(conn, tables)
        versions = _read_versions(conn, tables)
    problems = []
    for name, want in sorted(manifest["tables"].items()):
        if counts.get(name) != want:
            problems.append(f"table {name}: {counts.get(name, 'missing')} rows, manifest {want}")
    problems += _diff_rows("item", manifest["items"], items)
    problems += _diff_rows("repodoc version", manifest.get("repodoc_versions", []), versions)
    return problems


def verify(snapshot: str | os.PathLike[str]) -> Report:
    """Check a snapshot: dump against the manifest, and every named blob's sha256."""
    snap = Path(snapshot).expanduser()
    manifest = _load_manifest(snap)
    problems = _check_dump(snap, manifest) + _check_blobs(snap.parent.parent, manifest)
    return Report(not problems, manifest["item_count"], problems, str(snap))


def _require_empty_database(conninfo: str) -> None:
    psycopg = _psycopg()
    try:
        with psycopg.connect(conninfo, connect_timeout=10) as conn:
            tables = _table_names(conn)
    except psycopg.OperationalError as exc:
        raise BackupError(f"cannot connect to the restore database: {exc}") from exc
    if tables:
        raise BackupError("restore database is not empty; refusing to overwrite")


def restore(
    snapshot: str | os.PathLike[str],
    target: str | os.PathLike[str],
    *,
    dsn: str | None = None,
) -> Report:
    """Restore into an empty database (``dsn``) and an empty ``target`` files tree; verify."""
    snap = Path(snapshot).expanduser()
    out = Path(target).expanduser()
    manifest = _load_manifest(snap)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise BackupError(f"restore target {out} is not empty; refusing to overwrite")
    conninfo = dsn if dsn is not None else conninfo_from_env()
    _require_empty_database(conninfo)
    root = snap.parent.parent
    pre = _check_dump(snap, manifest) + _check_blobs(root, manifest)
    if pre:
        raise BackupError("snapshot failed verification: " + "; ".join(pre))
    out.mkdir(parents=True, exist_ok=True)
    _pg_restore(conninfo, snap / DUMP_FILE)
    for sha in sorted(_expected_blobs(manifest)):
        dst = _blob(out, sha)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_blob(root, sha), dst)
    problems = _check_database(conninfo, manifest) + _check_blobs(out, manifest)
    return Report(not problems, manifest["item_count"], problems, str(out))


def last_success(root: str | os.PathLike[str]) -> dict | None:
    """The newest complete snapshot's ``{snapshot, created_at, items, tables}``, else None.

    A snapshot only has a manifest once it finished, so a failed run never counts.
    """
    snaps = list_snapshots(root)
    if not snaps:
        return None
    manifest = json.loads((snaps[-1] / MANIFEST).read_text())
    return {
        "snapshot": str(snaps[-1]),
        "created_at": manifest.get("created_at"),
        "items": manifest.get("item_count", 0),
        "snapshots_kept": len(snaps),
    }


SERVICE_NAME = "culture-library-backup.service"
TIMER_NAME = "culture-library-backup.timer"
ENV_FILE = "%h/.config/culture-library/live.env"


def _command(data_dir: str, dest: str) -> str:
    return f"culture-library backup run --data-dir {data_dir} --to {dest}"


def unit_files(data_dir: str, dest: str, *, command: str | None = None) -> dict[str, str]:
    """``{unit file name: text}`` for a daily systemd user service + timer."""
    cmd = command or _command(data_dir, dest)
    service = (
        "[Unit]\nDescription=culture-library backup\n\n"
        f"[Service]\nType=oneshot\nEnvironmentFile=-{ENV_FILE}\nExecStart={cmd}\n"
    )
    timer = (
        "[Unit]\nDescription=Daily culture-library backup\n\n"
        "[Timer]\nOnCalendar=daily\nRandomizedDelaySec=1h\nPersistent=true\n"
        f"Unit={SERVICE_NAME}\n\n[Install]\nWantedBy=timers.target\n"
    )
    return {SERVICE_NAME: service, TIMER_NAME: timer}


def systemctl_commands() -> list[str]:
    return [
        "systemctl --user daemon-reload",
        f"systemctl --user enable --now {TIMER_NAME}",
        "systemctl --user list-timers",
    ]


def install_units(
    data_dir: str, dest: str, *, unit_dir: str | os.PathLike[str] | None = None,
    command: str | None = None,
) -> dict:  # fmt: skip
    """Write the service and timer into the systemd *user* unit directory; enable nothing."""
    base = Path(unit_dir).expanduser() if unit_dir else Path("~/.config/systemd/user").expanduser()
    base.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in unit_files(data_dir, dest, command=command).items():
        (base / name).write_text(text)
        written.append(str(base / name))
    return {"written": written, "next": systemctl_commands()}


def schedule_text(data_dir: str, dest: str, *, kind: str = "systemd") -> str:
    """Return (never install) a daily schedule: a systemd service+timer pair or a cron line."""
    if kind == "cron":
        return f"17 3 * * * {_command(data_dir, dest)}\n"
    if kind != "systemd":
        raise BackupError("schedule kind must be systemd or cron")
    return "".join(f"# {name}\n{text}\n" for name, text in unit_files(data_dir, dest).items())
