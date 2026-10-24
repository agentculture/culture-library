"""``culture-library server`` — run the library HTTP server.

Provenance: the shape of this module is cited (cite-don't-import) from repo
agentculture/xteink, commit ea2f643 (branch feat/issue-1), path
xteink/cli/_commands/server.py. This copy is owned and modified here.

FastAPI/uvicorn come from the optional ``[server]`` extra and are imported
lazily inside ``serve`` so the rest of the CLI works without them.
"""

from __future__ import annotations

import argparse

from culture_library.cli._commands.overview import emit_overview
from culture_library.cli._errors import EXIT_ENV_ERROR, CliError
from culture_library.server import DEFAULT_HOST, DEFAULT_PORT, ServerConfig

_JSON_HELP = "Emit structured JSON."


def _json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "culture-library server",
        [
            {
                "title": "Verbs",
                "items": [
                    "serve — start the HTTP server (needs the [server] extra)",
                    "overview — this description",
                ],
            },
            {
                "title": "Configuration",
                "items": [
                    f"--host (default {DEFAULT_HOST})",
                    f"--port (default {DEFAULT_PORT})",
                    "install: pip install 'culture-library[server]'",
                ],
            },
        ],
        json_mode=_json(args),
    )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        from culture_library.server import app as server_app
    except ImportError as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            f"server dependencies are not installed ({exc})",
            "install culture-library[server]",
        ) from None
    server_app.serve(ServerConfig(host=args.host, port=args.port))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("server", help="HTTP server (see 'culture-library server overview').")
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=cmd_overview, json=False)
    nsub = p.add_subparsers(dest="server_command", parser_class=type(p))
    sv = nsub.add_parser("serve", help="Start the HTTP server.")
    sv.add_argument("--host", default=DEFAULT_HOST, help=f"Bind address (default {DEFAULT_HOST}).")
    sv.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port.")
    sv.add_argument("--json", action="store_true", help=_JSON_HELP)
    sv.set_defaults(func=cmd_serve)
    ov = nsub.add_parser("overview", help="Describe the server noun.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_overview)
