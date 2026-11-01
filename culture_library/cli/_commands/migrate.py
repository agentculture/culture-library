"""``culture-library migrate`` — one-off migrations into the catalog."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from culture_library.cli._commands.overview import emit_overview
from culture_library.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from culture_library.cli._output import emit_error, emit_result
from culture_library.core.store import DATA_DIR_ENV, DEFAULT_DATA_DIR

_JSON_HELP = "Emit structured JSON."


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
                    "sqlite-to-postgres — copy a 0.19.8 SQLite library.db into an empty "
                    "Postgres, then verify it table by table (--verify-only compares again)",
                    "overview — this description",
                ],
            },
            {
                "title": "Safety",
                "items": [
                    "the xteink source is opened read-only and never written",
                    "ids, sha256 and created_at are preserved; re-runs are idempotent",
                    "sqlite-to-postgres reads the SQLite file read-only, refuses a non-empty "
                    "target, makes no embedding call, and commits only a verified copy",
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


def cmd_sqlite_to_postgres(args: argparse.Namespace) -> int:
    from culture_library.core.migrations import MigrationError as SchemaError
    from culture_library.core.search import EmbeddingError
    from culture_library.core.store import DatabaseError, conninfo_from_env, with_env_password
    from culture_library.migrate import sqlite_to_pg as s2p

    source = Path(args.source).expanduser()
    if not source.is_file():
        raise CliError(
            EXIT_USER_ERROR,
            f"no SQLite library at {source}",
            "point --from at the 0.19.8 library.db (a copy taken with the library stopped)",
        )
    try:
        conninfo = with_env_password(args.to) if args.to else conninfo_from_env()
        embedder = s2p.embedder_from_env(url=args.gateway_url, model=args.embed_model)
        run = s2p.verify_library if args.verify_only else s2p.copy_library
        report = run(
            source, conninfo, embedder=embedder, drop_stale_vectors=args.drop_stale_vectors
        )
    except s2p.TargetNotEmptyError as exc:
        raise CliError(
            EXIT_USER_ERROR,
            str(exc),
            "copy into a new, empty database (or check an earlier copy with --verify-only)",
        ) from None
    except s2p.SourceError as exc:
        raise CliError(
            EXIT_USER_ERROR, str(exc), "copy from the 0.19.8 library.db, unmodified"
        ) from None
    except EmbeddingError as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            str(exc),
            "the copy needs the gateway's /capabilities to name the vectors' model; check "
            "LOBES_GATEWAY_URL (or --gateway-url) and LOBES_EMBED_MODEL",
        ) from None
    except (s2p.CopyError, DatabaseError, SchemaError) as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            str(exc),
            "check the CULTURE_LIBRARY_PG_* settings (or --to) and that Postgres is running",
        ) from None
    emit_result(report.to_dict() if _json(args) else report.render(), json_mode=_json(args))
    if report.ok:
        return 0
    if report.problems:
        hint = (
            "fix or remove the listed rows in a copy of library.db, then copy again; "
            "stale vectors may be left to the indexer with --drop-stale-vectors"
        )
        message = f"{report.problem_count} source rows cannot be written to Postgres"
    else:
        hint = "see the report: the tables marked DIFF differ between SQLite and Postgres"
        message = "the target does not match the source"
    emit_error(CliError(EXIT_USER_ERROR, message, hint), json_mode=_json(args))
    return EXIT_USER_ERROR


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("migrate", help="Migrations (see 'culture-library migrate overview').")
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
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
    fx.add_argument("--json", action="store_true", help=_JSON_HELP)
    fx.set_defaults(func=cmd_from_xteink)
    sp = nsub.add_parser(
        "sqlite-to-postgres",
        help="Copy a 0.19.8 SQLite library.db into an empty Postgres and verify it.",
    )
    sp.add_argument(
        "--from", dest="source", required=True, help="The SQLite library.db (read-only)."
    )
    sp.add_argument(
        "--to",
        help="Target libpq DSN (default: the CULTURE_LIBRARY_PG_* settings). Keep the "
        "password in CULTURE_LIBRARY_PG_PASSWORD, not here.",
    )
    sp.add_argument("--gateway-url", help="Gateway for /capabilities (default $LOBES_GATEWAY_URL).")
    sp.add_argument("--embed-model", help="Embedder model name (default $LOBES_EMBED_MODEL).")
    sp.add_argument(
        "--drop-stale-vectors",
        action="store_true",
        help="Skip vectors made under another model label (the indexer re-embeds them).",
    )
    sp.add_argument(
        "--verify-only",
        action="store_true",
        help="Copy nothing; compare an already copied target with the source.",
    )
    sp.add_argument("--json", action="store_true", help=_JSON_HELP)
    sp.set_defaults(func=cmd_sqlite_to_postgres)
    ov = nsub.add_parser("overview", help="Describe the migrate noun.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_overview)
