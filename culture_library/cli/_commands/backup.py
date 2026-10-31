"""``culture-library backup`` — snapshot, verify and restore the library."""

from __future__ import annotations

import argparse
import os

from culture_library.cli._commands.overview import emit_overview
from culture_library.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from culture_library.cli._output import emit_result
from culture_library.core.backup import BACKUP_DIR_ENV, DEFAULT_KEEP, PG_BIN_DIR_ENV
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


def _last_success_section(args: argparse.Namespace) -> dict:
    from culture_library.core import backup

    dest = args.to or os.environ.get(BACKUP_DIR_ENV)
    if not dest:
        return {
            "title": "Last success",
            "items": [f"no backup location (--to or ${BACKUP_DIR_ENV})"],
        }
    last = backup.last_success(dest)
    if last is None:
        return {"title": "Last success", "items": [f"none yet in {dest}"]}
    return {
        "title": "Last success",
        "items": [
            f"{last['created_at']} - {last['snapshot']} ({last['items']} items; "
            f"{last['snapshots_kept']} snapshots kept)"
        ],
    }


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "culture-library backup",
        [
            {
                "title": "Verbs",
                "items": [
                    "run [--to D] [--keep N] - pg_dump snapshot + incremental files copy to a "
                    "second location",
                    "verify <snapshot> - check the dump, manifest and every blob's sha256",
                    "restore --from <snapshot> --to <empty dir> [--dsn DSN] - pg_restore into "
                    "an empty database and files tree, then verify",
                    "schedule [--to D] [--unit-dir U] - write a daily systemd user timer "
                    "(enables nothing); --print only prints (systemd or cron)",
                    "overview [--to D] - this description and the last successful backup",
                ],
            },
            {
                "title": "Safety",
                "items": [
                    "safe while the server writes (one exported Postgres snapshot)",
                    "restore refuses a non-empty database or files target and never overwrites",
                    "needs pg_dump / pg_restore 17 on PATH or in $CULTURE_LIBRARY_PG_BIN_DIR",
                ],
            },
            _last_success_section(args),
        ],
        json_mode=_json(args),
    )
    return 0


def _wrap(fn, *a, **kw):
    from culture_library.core.backup import BackupError, PgToolMissing
    from culture_library.core.errors import CoreError

    try:
        return fn(*a, **kw)
    except PgToolMissing as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            str(exc),
            f"install the PostgreSQL 17 client tools or set {PG_BIN_DIR_ENV}",
        ) from None
    except (BackupError, CoreError) as exc:
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

    snap = _wrap(backup.run_backup, _data_dir(args), _dest(args), keep=args.keep, dsn=args.dsn)
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

    return _report_cmd(args, _wrap(backup.restore, args.source, args.to, dsn=args.dsn), "restore")


def cmd_schedule(args: argparse.Namespace) -> int:
    from culture_library.core import backup

    if args.print_only:
        text = _wrap(
            backup.schedule_text, _data_dir(args), args.to or f"${BACKUP_DIR_ENV}", kind=args.kind
        )
        emit_result(
            {"kind": args.kind, "text": text} if _json(args) else text, json_mode=_json(args)
        )
        return 0
    if args.kind != "systemd":
        raise CliError(
            EXIT_USER_ERROR,
            "only systemd units can be written; cron is print-only",
            "use --print --kind cron and install the line yourself",
        )
    res = _wrap(
        backup.install_units,
        _data_dir(args),
        _dest(args),
        unit_dir=args.unit_dir,
        command=args.exec_command,
    )
    lines = [f"wrote {path}" for path in res["written"]]
    lines += ["enable it yourself (nothing was enabled or started):"]
    lines += [f"  {cmd}" for cmd in res["next"]]
    emit_result(res if _json(args) else "\n".join(lines), json_mode=_json(args))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("backup", help="Backups (see 'culture-library backup overview').")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.add_argument("--to", help=f"Backup location (default ${BACKUP_DIR_ENV}).")
    p.set_defaults(func=cmd_overview, json=False)
    nsub = p.add_subparsers(dest="backup_command", parser_class=type(p))

    def common(q: argparse.ArgumentParser) -> None:
        q.add_argument("--json", action="store_true", help="Emit structured JSON.")

    r = nsub.add_parser("run", help="Snapshot the library to a second location.")
    r.add_argument("--data-dir", help=f"Library data dir (default ${DATA_DIR_ENV}).")
    r.add_argument("--to", help=f"Backup location (default ${BACKUP_DIR_ENV}).")
    r.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="Snapshots to keep (7).")
    r.add_argument("--dsn", help="Library database DSN (default: the CULTURE_LIBRARY_PG_* env).")
    common(r)
    r.set_defaults(func=cmd_run)

    v = nsub.add_parser("verify", help="Verify a snapshot.")
    v.add_argument("snapshot", help="Snapshot directory.")
    common(v)
    v.set_defaults(func=cmd_verify)

    rs = nsub.add_parser("restore", help="Restore a snapshot into an empty directory.")
    rs.add_argument("--from", dest="source", required=True, help="Snapshot directory.")
    rs.add_argument("--to", required=True, help="Empty target data directory (files tree).")
    rs.add_argument("--dsn", help="EMPTY database to restore into (default: the PG env).")
    common(rs)
    rs.set_defaults(func=cmd_restore)

    s = nsub.add_parser("schedule", help="Write a daily systemd user timer for the backup.")
    s.add_argument("--print", dest="print_only", action="store_true", help="Print, write nothing.")
    s.add_argument("--unit-dir", help="Where to write the units (~/.config/systemd/user).")
    s.add_argument(
        "--command",
        dest="exec_command",
        help="ExecStart command (default: culture-library backup run).",
    )
    s.add_argument("--kind", choices=("systemd", "cron"), default="systemd")
    s.add_argument("--data-dir", help=f"Library data dir (default ${DATA_DIR_ENV}).")
    s.add_argument("--to", help="Backup location to embed.")
    common(s)
    s.set_defaults(func=cmd_schedule)

    ov = nsub.add_parser("overview", help="Describe the backup noun and its last success.")
    ov.add_argument("--to", help=f"Backup location (default ${BACKUP_DIR_ENV}).")
    common(ov)
    ov.set_defaults(func=cmd_overview)
