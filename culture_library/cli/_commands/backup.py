"""``culture-library backup`` — snapshot, verify and restore the library."""

from __future__ import annotations

import argparse
import os

from culture_library.cli._commands.overview import emit_overview
from culture_library.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from culture_library.cli._output import emit_result
from culture_library.core.backup import BACKUP_DIR_ENV, DEFAULT_KEEP
from culture_library.core.store import DATA_DIR_ENV, DEFAULT_DATA_DIR


def _json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _data_dir(args: argparse.Namespace) -> str:
    return args.data_dir or os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR


def _dest(args: argparse.Namespace) -> str:
    dest = args.to or os.environ.get(BACKUP_DIR_ENV)
    if not dest:
        raise CliError(
            EXIT_USER_ERROR,
            "no backup location configured",
            f"pass --to <dir> or set {BACKUP_DIR_ENV}",
        )
    return dest


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "culture-library backup",
        [
            {
                "title": "Verbs",
                "items": [
                    "run [--to D] [--keep N] — consistent SQLite snapshot + incremental "
                    "files copy to a second location",
                    "verify <snapshot> — check the snapshot against its manifest and sha256s",
                    "restore --from <snapshot> --to <empty dir> — restore and verify",
                    "schedule --print [--kind systemd|cron] — print a daily schedule "
                    "(installs nothing)",
                    "overview — this description",
                ],
            },
            {
                "title": "Safety",
                "items": [
                    "safe while the server writes (SQLite online backup API, WAL)",
                    "restore refuses a non-empty target and never overwrites",
                ],
            },
        ],
        json_mode=_json(args),
    )
    return 0


def _wrap(fn, *a, **kw):
    from culture_library.core.backup import BackupError

    try:
        return fn(*a, **kw)
    except BackupError as exc:
        raise CliError(EXIT_USER_ERROR, str(exc), "see 'culture-library explain backup'") from None
    except OSError as exc:
        raise CliError(
            EXIT_ENV_ERROR, str(exc), "check paths and permissions of both locations"
        ) from None


def _render_report(rep, verb: str) -> str:
    lines = [f"{verb}: {'OK' if rep.ok else 'FAILED'} ({rep.items} items) {rep.target}"]
    lines += [f"  problem: {p}" for p in rep.problems]
    return "\n".join(lines)


def cmd_run(args: argparse.Namespace) -> int:
    from culture_library.core import backup

    snap = _wrap(backup.run_backup, _data_dir(args), _dest(args), keep=args.keep)
    text = (
        f"snapshot {snap.path} ({snap.manifest['item_count']} items, "
        f"{snap.blobs_copied} new blobs of {snap.blobs_total}; pruned {len(snap.pruned)})"
    )
    emit_result(snap.to_dict() if _json(args) else text, json_mode=_json(args))
    return 0


def _report_cmd(args: argparse.Namespace, rep, verb: str) -> int:
    emit_result(rep.to_dict() if _json(args) else _render_report(rep, verb), json_mode=_json(args))
    return 0 if rep.ok else 1


def cmd_verify(args: argparse.Namespace) -> int:
    from culture_library.core import backup

    return _report_cmd(args, _wrap(backup.verify, args.snapshot), "verify")


def cmd_restore(args: argparse.Namespace) -> int:
    from culture_library.core import backup

    return _report_cmd(args, _wrap(backup.restore, args.source, args.to), "restore")


def cmd_schedule(args: argparse.Namespace) -> int:
    from culture_library.core import backup

    if not args.print_only:
        raise CliError(
            EXIT_USER_ERROR,
            "schedule only prints; it never installs anything",
            "re-run with --print and install the output yourself",
        )
    text = _wrap(
        backup.schedule_text, _data_dir(args), args.to or f"${BACKUP_DIR_ENV}", kind=args.kind
    )
    emit_result({"kind": args.kind, "text": text} if _json(args) else text, json_mode=_json(args))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("backup", help="Backups (see 'culture-library backup overview').")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_overview, json=False)
    nsub = p.add_subparsers(dest="backup_command", parser_class=type(p))

    def common(q: argparse.ArgumentParser) -> None:
        q.add_argument("--json", action="store_true", help="Emit structured JSON.")

    r = nsub.add_parser("run", help="Snapshot the library to a second location.")
    r.add_argument("--data-dir", help=f"Library data dir (default ${DATA_DIR_ENV}).")
    r.add_argument("--to", help=f"Backup location (default ${BACKUP_DIR_ENV}).")
    r.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="Snapshots to keep (7).")
    common(r)
    r.set_defaults(func=cmd_run)

    v = nsub.add_parser("verify", help="Verify a snapshot.")
    v.add_argument("snapshot", help="Snapshot directory.")
    common(v)
    v.set_defaults(func=cmd_verify)

    rs = nsub.add_parser("restore", help="Restore a snapshot into an empty directory.")
    rs.add_argument("--from", dest="source", required=True, help="Snapshot directory.")
    rs.add_argument("--to", required=True, help="Empty target data directory.")
    common(rs)
    rs.set_defaults(func=cmd_restore)

    s = nsub.add_parser("schedule", help="Print a daily backup schedule.")
    s.add_argument("--print", dest="print_only", action="store_true", help="Print (required).")
    s.add_argument("--kind", choices=("systemd", "cron"), default="systemd")
    s.add_argument("--data-dir", help=f"Library data dir (default ${DATA_DIR_ENV}).")
    s.add_argument("--to", help="Backup location to embed.")
    common(s)
    s.set_defaults(func=cmd_schedule)

    ov = nsub.add_parser("overview", help="Describe the backup noun.")
    common(ov)
    ov.set_defaults(func=cmd_overview)
