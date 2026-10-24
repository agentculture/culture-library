"""``culture-library migrate`` — one-off migrations into the catalog."""

from __future__ import annotations

import argparse
import os

from culture_library.cli._commands.overview import emit_overview
from culture_library.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from culture_library.cli._output import emit_result
from culture_library.core.store import DATA_DIR_ENV, DEFAULT_DATA_DIR


def _json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "culture-library migrate",
        [
            {
                "title": "Verbs",
                "items": [
                    "from-xteink — copy an xteink library into this catalog "
                    "(dry run unless --apply)",
                    "overview — this description",
                ],
            },
            {
                "title": "Safety",
                "items": [
                    "the xteink source is opened read-only and never written",
                    "ids, sha256 and created_at are preserved; re-runs are idempotent",
                ],
            },
        ],
        json_mode=_json(args),
    )
    return 0


def _render(report) -> str:
    mode = "APPLIED" if report.apply else "DRY RUN"
    lines = [f"{mode} (visibility={report.visibility})"]
    for r in report.rows:
        lines.append(
            f"{r.id}\t{r.sha256[:12]}\t{r.old_kind} -> {r.new_kind}\t{r.status}\t{r.title}"
        )
    lines.append(f"{report.matched}/{report.total} items with matching sha256")
    return "\n".join(lines)


def cmd_from_xteink(args: argparse.Namespace) -> int:
    from pathlib import Path

    from culture_library.migrate.xteink import (
        MigrationConflict,
        MigrationError,
        migrate_from_xteink,
    )

    source = Path(args.data_dir).expanduser()
    if not (source / "library.db").is_file():
        raise CliError(
            EXIT_USER_ERROR,
            f"no library.db in {source}",
            "point --data-dir at the xteink data directory (contains library.db and files/)",
        )
    dest = args.dest or os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR
    try:
        report = migrate_from_xteink(
            source, Path(dest).expanduser(), apply=args.apply, visibility=args.visibility
        )
    except MigrationConflict as exc:
        raise CliError(
            EXIT_USER_ERROR,
            str(exc),
            "resolve the conflicting destination item; the migration never overwrites",
        ) from None
    except (MigrationError, OSError) as exc:
        raise CliError(
            EXIT_ENV_ERROR, str(exc), "check the xteink data directory is intact and readable"
        ) from None
    emit_result(report.to_dict() if _json(args) else _render(report), json_mode=_json(args))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("migrate", help="Migrations (see 'culture-library migrate overview').")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_overview, json=False)
    nsub = p.add_subparsers(dest="migrate_command", parser_class=type(p))
    fx = nsub.add_parser("from-xteink", help="Copy an xteink library (dry run unless --apply).")
    fx.add_argument("--data-dir", required=True, help="xteink data dir (library.db + files/).")
    fx.add_argument("--dest", help=f"Destination data dir (default ${DATA_DIR_ENV}).")
    fx.add_argument("--apply", action="store_true", help="Actually copy; default is a dry run.")
    fx.add_argument(
        "--visibility",
        choices=("private", "public"),
        default="private",
        help="Visibility for migrated items (default private).",
    )
    fx.add_argument("--json", action="store_true", help="Emit structured JSON.")
    fx.set_defaults(func=cmd_from_xteink)
    ov = nsub.add_parser("overview", help="Describe the migrate noun.")
    ov.add_argument("--json", action="store_true", help="Emit structured JSON.")
    ov.set_defaults(func=cmd_overview)
