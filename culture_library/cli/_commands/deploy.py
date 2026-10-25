"""``culture-library deploy`` — recreate the live stack's library and mcp containers.

``deploy restart`` is the CLI seam for the runbook's "Restart the live stack"
(``docs/operations/deploy.md``): it checks the host env file first (every required
setting present, no placeholder, send targets that parse), then runs, with a fixed argv
and no shell::

    grant run --inject TUNNEL_TOKEN=<secret> -- docker compose -f deploy/compose.yaml \\
      --env-file <env file> --profile remote up -d --no-deps library mcp

and checks ``/api/health`` and the send targets inside the new container. ``--build``
rebuilds the image first; ``--dry-run`` prints the commands and runs nothing. The tunnel
token stays in ``grant`` and never passes through this process.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv, no shell
import time
import urllib.request
from pathlib import Path

from culture_library.cli._commands.overview import emit_overview
from culture_library.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from culture_library.cli._output import emit_diagnostic, emit_result
from culture_library.core import send

LIVE_ENV_VAR = "CULTURE_LIBRARY_LIVE_ENV"
# The grant secret's *name*; the token itself never enters this process.
DEFAULT_TUNNEL_SECRET = "CULTUREFLARE_LIBRARY_CULTURE_DEV_TUNNEL_TOKEN"  # nosec B105
REQUIRED_SETTINGS = (
    "COMPOSE_PROJECT_NAME",
    "CULTURE_LIBRARY_ACCESS_TEAM_DOMAIN",
    "CULTURE_LIBRARY_ACCESS_AUD",
    send.TARGETS_ENV,
)
SERVICES = ("library", "mcp")
DEFAULT_PORT = "8796"
STEP_TIMEOUT = 900
_RUNBOOK = "docs/operations/deploy.md, 'Restart the live stack'"
_PLACEHOLDER = re.compile(r"<[^>]*>|\.\.\.")
_TARGETS_PROBE = (
    "import json; from culture_library.core import send; "
    "print(json.dumps([t.label for t in send.load_targets('/data')]))"
)

# Seams for tests.
_run = subprocess.run
_which = shutil.which


def _json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def default_env_file() -> Path:
    """``$CULTURE_LIBRARY_LIVE_ENV``, else ``$HOME/.config/culture-library/live.env``."""
    raw = os.environ.get(LIVE_ENV_VAR)
    return Path(raw) if raw else Path.home() / ".config" / "culture-library" / "live.env"


def _default_compose() -> Path:
    checkout = Path(__file__).resolve().parents[3] / "deploy" / "compose.yaml"
    return checkout if checkout.is_file() else Path.cwd() / "deploy" / "compose.yaml"


def read_env_file(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` lines as compose reads them: ``#`` comments, quotes stripped."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def check_settings(values: dict[str, str], source: str) -> None:
    """Refuse a missing, empty or placeholder setting, or send targets that do not parse."""
    for key in REQUIRED_SETTINGS:
        value = values.get(key, "")
        if not value.strip():
            raise CliError(
                EXIT_USER_ERROR, f"{source}: {key} is missing or empty", f"set it; see {_RUNBOOK}"
            )
        if key != send.TARGETS_ENV and _PLACEHOLDER.search(value):
            raise CliError(
                EXIT_USER_ERROR,
                f"{source}: {key} looks like a placeholder ({value!r})",
                "put the real value in the env file",
            )
    try:
        send.parse_targets(values[send.TARGETS_ENV], f"{source}: {send.TARGETS_ENV}")
    except send.SendConfigError as exc:
        raise CliError(
            EXIT_USER_ERROR,
            str(exc),
            "write the targets as a JSON list in single quotes, e.g. "
            '\'[{"label":"x3","type":"reader","enabled":true}]\'',
        ) from None


def plan(compose: Path, env_file: Path, secret: str, *, build: bool) -> list[list[str]]:
    """The commands ``restart`` runs, in order."""
    base = ["docker", "compose", "-f", str(compose), "--env-file", str(env_file)]
    steps = [base + ["build", "library"]] if build else []
    up = base + ["--profile", "remote", "up", "-d", "--no-deps", *SERVICES]
    steps.append(["grant", "run", "--inject", f"TUNNEL_TOKEN={secret}", "--", *up])
    return steps


def _step(argv: list[str]) -> subprocess.CompletedProcess:
    if _which(argv[0]) is None:
        raise CliError(EXIT_ENV_ERROR, f"{argv[0]} is not on PATH", f"install {argv[0]}")
    proc = _run(  # nosec B603 - argv is assembled from fixed literals and checked paths
        argv, capture_output=True, text=True, timeout=STEP_TIMEOUT, check=False
    )
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        raise CliError(
            EXIT_ENV_ERROR,
            f"{argv[0]} {argv[1]} exited {proc.returncode}: {output[-800:]}",
            "read the output above; the previous containers keep running if 'up' failed",
        )
    if output:
        emit_diagnostic(output)
    return proc


def _health(url: str, timeout: float) -> dict:
    """Poll *url* until it answers ``status: ok`` or *timeout* seconds pass."""
    deadline = time.monotonic() + timeout
    last = "no answer"
    while True:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:  # nosec B310 - http loopback
                body = json.loads(resp.read().decode("utf-8"))
            if body.get("status") == "ok":
                return body
            last = str(body)
        except (OSError, ValueError) as exc:
            last = str(exc)
        if time.monotonic() >= deadline:
            raise CliError(
                EXIT_ENV_ERROR,
                f"{url} did not report healthy within {timeout:.0f}s ({last})",
                "check 'docker compose -f deploy/compose.yaml logs library'",
            )
        time.sleep(2)


def cmd_restart(args: argparse.Namespace) -> int:
    env_file = Path(args.env_file) if args.env_file else default_env_file()
    if not env_file.is_file():
        raise CliError(
            EXIT_USER_ERROR,
            f"env file {env_file} does not exist",
            f"create it as described in {_RUNBOOK}, or pass --env-file",
        )
    compose = Path(args.compose) if args.compose else _default_compose()
    if not compose.is_file():
        raise CliError(
            EXIT_USER_ERROR,
            f"compose file {compose} does not exist",
            "run from the culture-library checkout, or pass --compose",
        )
    values = read_env_file(env_file)
    check_settings(values, str(env_file))
    steps = plan(compose, env_file, args.tunnel_secret, build=args.build)
    if args.dry_run:
        emit_result(
            (
                {"dry_run": True, "steps": steps}
                if _json(args)
                else "\n".join(" ".join(s) for s in steps)
            ),
            json_mode=_json(args),
        )
        return 0
    for argv in steps:
        _step(argv)
    port = values.get("LIBRARY_PUBLISH_PORT") or DEFAULT_PORT
    health = _health(f"http://127.0.0.1:{port}/api/health", args.timeout)
    probe = _step(
        ["docker", "compose", "-f", str(compose), "--env-file", str(env_file)]
        + ["exec", "-T", "library", "python3", "-c", _TARGETS_PROBE]
    )
    targets = json.loads(probe.stdout.strip().splitlines()[-1])
    result = {"dry_run": False, "steps": steps, "health": health, "send_targets": targets}
    text = (
        f"restarted {', '.join(SERVICES)}: version {health.get('version')}, "
        f"send targets {', '.join(targets) or '(none)'}"
    )
    emit_result(result if _json(args) else text, json_mode=_json(args))
    return 0


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "culture-library deploy",
        [
            {
                "title": "Verbs",
                "items": [
                    "restart — recreate library + mcp from the host env file, then check them",
                    "overview — this description",
                ],
            },
            {
                "title": "Configuration",
                "items": [
                    f"--env-file (default ${LIVE_ENV_VAR}, else "
                    "$HOME/.config/culture-library/live.env)",
                    "--compose (default deploy/compose.yaml in this checkout)",
                    f"--tunnel-secret (grant secret name, default {DEFAULT_TUNNEL_SECRET})",
                    "needs grant and docker on PATH",
                ],
            },
        ],
        json_mode=_json(args),
    )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("deploy", help="Live stack (see 'culture-library deploy overview').")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_overview, json=False)
    nsub = p.add_subparsers(dest="deploy_command", parser_class=type(p))
    rs = nsub.add_parser("restart", help="Recreate library + mcp from the host env file.")
    rs.add_argument("--env-file", help="Host env file (default: see 'deploy overview').")
    rs.add_argument("--compose", help="Compose file (default deploy/compose.yaml).")
    rs.add_argument(
        "--tunnel-secret", default=DEFAULT_TUNNEL_SECRET, help="grant secret holding the token."
    )
    rs.add_argument("--build", action="store_true", help="Rebuild the image first.")
    rs.add_argument("--dry-run", action="store_true", help="Print the commands; run nothing.")
    rs.add_argument(
        "--timeout", type=float, default=90.0, help="Seconds to wait for health (default 90)."
    )
    rs.add_argument("--json", action="store_true", help="Emit structured JSON.")
    rs.set_defaults(func=cmd_restart)
    ov = nsub.add_parser("overview", help="Describe the deploy noun.")
    ov.add_argument("--json", action="store_true", help="Emit structured JSON.")
    ov.set_defaults(func=cmd_overview)
