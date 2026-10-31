"""``culture-library mirror`` — inspect and reconcile the repo-document S3 mirror.

Like ``backup``, the verbs open the library store directly (``--data-dir``, else
``CULTURE_LIBRARY_DATA_DIR``); they do not go through the HTTP server. ``reconcile`` needs a
backend, built from ``CULTURE_LIBRARY_S3_BUCKET`` (and ``_REGION``/``_PREFIX``) with boto3 from
the ``[s3]`` extra; it only queues outbox rows, and the server's mirror worker uploads them.
"""

from __future__ import annotations

import argparse
import os

from culture_library.cli._commands.overview import emit_overview
from culture_library.cli._errors import EXIT_ENV_ERROR, CliError
from culture_library.cli._output import emit_result
from culture_library.core.store import DATA_DIR_ENV, DEFAULT_DATA_DIR

BUCKET_ENV = "CULTURE_LIBRARY_S3_BUCKET"


def _json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _store(args: argparse.Namespace):
    from culture_library.core import Store
    from culture_library.core import mirror as _mirror  # noqa: F401  (registers the outbox)

    data_dir = args.data_dir or os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR
    try:
        return Store(data_dir)
    except OSError as exc:
        raise CliError(
            EXIT_ENV_ERROR, f"cannot open the library at {data_dir}: {exc}", "check --data-dir"
        ) from None


def _backend_from_env():
    """The configured S3 backend (tests monkeypatch this to inject a fake)."""
    from culture_library.storage import s3

    return s3.from_env()


def _backend():
    if not os.environ.get(BUCKET_ENV, ""):
        raise CliError(
            EXIT_ENV_ERROR,
            "no mirror backend configured",
            f"set {BUCKET_ENV} (and AWS credentials), then re-run",
        )
    try:
        return _backend_from_env()
    except ImportError as exc:
        raise CliError(EXIT_ENV_ERROR, str(exc), "install the extra: uv sync --extra s3") from None
    except Exception as exc:  # noqa: BLE001 - any setup failure is an environment problem
        raise CliError(
            EXIT_ENV_ERROR,
            f"cannot build the S3 backend: {type(exc).__name__}: {exc}",
            f"check {BUCKET_ENV}, CULTURE_LIBRARY_S3_REGION and the AWS credentials",
        ) from None


def _outbox(store) -> dict:
    """Outbox figures; ``last_error`` matches ``/api/status`` (newest error among pending and
    parked rows)."""
    from culture_library.core import mirror as mr

    stats = mr.pending_stats(store)
    with store.reader() as conn:
        done = conn.execute("SELECT COUNT(*) FROM mirror_outbox WHERE state = 'done'").fetchone()
        err = conn.execute(
            "SELECT last_error FROM mirror_outbox WHERE state IN ('pending', 'failed')"
            " AND last_error != '' ORDER BY enqueued_at DESC, id DESC LIMIT 1"
        ).fetchone()
    return {
        "pending": stats["count"],
        "parked": stats["parked"],
        "done": done[0],
        "oldest_pending_age_s": stats["oldest_age_seconds"],
        "last_error": err[0] if err else None,
    }


def _max_attempts() -> int:
    from culture_library.core import mirror as mr

    return mr.MAX_ATTEMPTS


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "culture-library mirror",
        [
            {
                "title": "Verbs",
                "items": [
                    "status [--data-dir D] — outbox backlog (pending, parked, done, oldest "
                    "pending age, newest error) and whether a backend is configured",
                    "reconcile [--data-dir D] — compare every repo-document version/draft with "
                    "the bucket and queue the missing or mismatched keys, parked ones included "
                    f"(needs {BUCKET_ENV} and the [s3] extra)",
                    "overview — this description",
                ],
            },
            {
                "title": "How it works",
                "items": [
                    "every push queues an outbox row; the server's mirror worker uploads it "
                    "with backoff while the bucket is unreachable",
                    f"a row that fails {_max_attempts()} times is parked so it stops delaying the "
                    "rest; status counts it and reconcile re-queues it",
                    "reconcile only queues rows; it never deletes objects",
                    "GET /api/status carries the same figures in its mirror block",
                ],
            },
        ],
        json_mode=_json(args),
    )
    return 0


def _render_age(age) -> str:
    return "-" if age is None else f"{age:.0f}s"


def cmd_status(args: argparse.Namespace) -> None:
    bucket = os.environ.get(BUCKET_ENV, "")
    body = {"configured": bool(bucket), "bucket": bucket or None, **_outbox(_store(args))}
    if _json(args):
        emit_result(body, json_mode=True)
        return
    lines = [
        f"backend: {'s3://' + bucket if bucket else f'not configured ({BUCKET_ENV} unset)'}",
        f"pending: {body['pending']} (oldest {_render_age(body['oldest_pending_age_s'])})",
        f"done: {body['done']}",
        f"parked: {body['parked']} (failed {_max_attempts()} times; reconcile re-queues them)",
        f"last error: {body['last_error'] or '-'}",
    ]
    emit_result("\n".join(lines), json_mode=False)


def cmd_reconcile(args: argparse.Namespace) -> int:
    from culture_library.core import mirror as mr

    backend = _backend()
    store = _store(args)
    try:
        enqueued = mr.reconcile(store, backend)
    # head() failures are the bucket's, not the user's
    except Exception as exc:  # noqa: BLE001
        raise CliError(
            EXIT_ENV_ERROR,
            f"reconcile failed: {type(exc).__name__}: {exc}",
            "check the bucket is reachable and the credentials can read it",
        ) from None
    body = {"enqueued": enqueued, **_outbox(store)}
    text = (
        f"enqueued {enqueued} missing or mismatched keys; {body['pending']} pending "
        "(the server's mirror worker uploads them)"
    )
    emit_result(body if _json(args) else text, json_mode=_json(args))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("mirror", help="S3 mirror (see 'culture-library mirror overview').")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_overview, json=False)
    nsub = p.add_subparsers(dest="mirror_command", parser_class=type(p))

    def common(q: argparse.ArgumentParser, data_dir: bool = True) -> None:
        if data_dir:
            q.add_argument("--data-dir", help=f"Library data dir (default ${DATA_DIR_ENV}).")
        q.add_argument("--json", action="store_true", help="Emit structured JSON.")

    st = nsub.add_parser("status", help="Show the mirror outbox backlog.")
    common(st)
    st.set_defaults(func=cmd_status)

    rc = nsub.add_parser("reconcile", help="Queue keys missing or stale in the bucket.")
    common(rc)
    rc.set_defaults(func=cmd_reconcile)

    ov = nsub.add_parser("overview", help="Describe the mirror noun.")
    common(ov, data_dir=False)
    ov.set_defaults(func=cmd_overview)
