"""``culture-library deploy`` — recreate the live stack's library and mcp containers.

``deploy restart`` is the CLI seam for the runbook's "Restart the live stack"
(``docs/operations/deploy.md``): it checks the host env file first (every required
setting present, no placeholder, send targets that parse), then runs, with a fixed argv
and no shell::

    grant run --inject TUNNEL_TOKEN=<secret> --inject GATEWAY_API_KEY=<secret> -- \\
      docker compose -f deploy/compose.yaml --env-file <env file> --profile remote \\
      up -d --no-deps library mcp

and checks ``/api/health`` and the send targets inside the new container. ``--build``
rebuilds the image first; ``--dry-run`` prints the commands and runs nothing. The tunnel
token and the gateway key stay in ``grant`` and never pass through this process; an env
file that contains ``GATEWAY_API_KEY`` is refused.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
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
# The grant secret holding the lobes gateway key (embeddings + reranker); never in live.env.
DEFAULT_GATEWAY_SECRET = "LOBES_GATEWAY_API_KEY"  # nosec B105
# Settings that are secrets: never in the env file, always injected from grant by name.
SECRET_SETTINGS = {
    "TUNNEL_TOKEN": DEFAULT_TUNNEL_SECRET,
    "GATEWAY_API_KEY": DEFAULT_GATEWAY_SECRET,
}
REQUIRED_SETTINGS = (
    "COMPOSE_PROJECT_NAME",
    "CULTURE_LIBRARY_ACCESS_TEAM_DOMAIN",
    "CULTURE_LIBRARY_ACCESS_AUD",
    send.TARGETS_ENV,
)
SERVICES = ("library", "mcp")
_EXPORT = "export "
DEFAULT_PORT = "8796"
STEP_TIMEOUT = 900
_RUNBOOK = "docs/operations/deploy.md, 'Restart the live stack'"
_JSON_HELP = "Emit structured JSON."
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


def _env_value(key: str, raw: str, source: str) -> str:
    """One value the way compose's env-file reader takes it, or refuse what we cannot check.

    Single quotes are literal; double quotes and unquoted values end at the closing quote or
    at an unquoted `` #`` comment. A ``$`` outside single quotes would be interpolated by
    compose, so the checked text would not be the value compose uses: that is refused.
    """
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        quote = raw[0]
        end = raw.find(quote, 1)
        value = raw[1:end] if end != -1 else raw[1:]
        literal = quote == "'"
    else:
        hit = re.search(r"\s#", raw)
        value = (raw[: hit.start()] if hit else raw).strip()
        literal = False
    if not literal and "$" in value:
        raise CliError(
            EXIT_USER_ERROR,
            f"{source}: {key} uses ${{...}} interpolation, which this check cannot follow",
            "write the literal value, or single-quote it",
        )
    return value


def read_env_file(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` lines read like compose's ``--env-file``: comments, ``export``, quotes."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        key = key.strip()
        key = key.removeprefix(_EXPORT).strip()
        values[key] = _env_value(key, raw, str(path))
    return values


def _placeholder(source: str, what: str, value: str) -> CliError:
    return CliError(
        EXIT_USER_ERROR,
        f"{source}: {what} looks like a placeholder ({value!r})",
        "put the real value in the env file",
    )


def env_file_keys(path: Path) -> dict[str, str]:
    """``KEY -> raw value`` for each assignment, before any quoting or interpolation check."""
    keys: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        key = key.strip().removeprefix(_EXPORT).strip()
        keys[key] = raw
    return keys


def _refuse_secrets(
    values: dict[str, str], source: str, secrets: dict[str, str] | None = None
) -> None:
    """Refuse an env file that sets a secret; the hint names the grant secret it comes from."""
    secrets = SECRET_SETTINGS if secrets is None else secrets
    for key, grant_name in secrets.items():
        if values.get(key, "").strip().strip("'\"").strip():
            raise CliError(
                EXIT_USER_ERROR,
                f"{source}: {key} is a secret and must not be in the env file",
                f"remove it; deploy restart injects it from grant ({grant_name})",
            )


def check_settings(values: dict[str, str], source: str) -> None:
    """Refuse a missing, empty or placeholder setting, send targets that do not parse, or a
    secret written into the (secret-free) env file."""
    _refuse_secrets(values, source)
    for key in REQUIRED_SETTINGS:
        value = values.get(key, "")
        if not value.strip():
            raise CliError(
                EXIT_USER_ERROR, f"{source}: {key} is missing or empty", f"set it; see {_RUNBOOK}"
            )
        if key != send.TARGETS_ENV and _PLACEHOLDER.search(value):
            raise _placeholder(source, key, value)
    try:
        targets = send.parse_targets(values[send.TARGETS_ENV], f"{source}: {send.TARGETS_ENV}")
    except send.SendConfigError as exc:
        raise CliError(
            EXIT_USER_ERROR,
            str(exc),
            "write the targets as a JSON list in single quotes, e.g. "
            '\'[{"label":"x3","type":"reader","enabled":true}]\'',
        ) from None
    for target in targets:
        for field in (target.label, target.type):
            if _PLACEHOLDER.search(field):
                raise _placeholder(source, f"{send.TARGETS_ENV} target", field)


def _child_env(values: dict[str, str]) -> dict[str, str]:
    """This process's environment minus every key the env file sets.

    Compose prefers a shell variable over ``--env-file``; dropping them makes the checked
    file the only source of those settings.
    """
    drop = set(values) | set(REQUIRED_SETTINGS) | set(SECRET_SETTINGS)
    return {k: v for k, v in os.environ.items() if k not in drop}


def _compose_base(compose: Path, env_file: Path) -> list[str]:
    return ["docker", "compose", "-f", str(compose), "--env-file", str(env_file)]


def plan(
    compose: Path,
    env_file: Path,
    secret: str,
    *,
    build: bool,
    gateway_secret: str | None = DEFAULT_GATEWAY_SECRET,
) -> list[list[str]]:
    """The commands ``restart`` runs, in order. Secrets are injected by name from grant."""
    base = _compose_base(compose, env_file)
    steps = [base + ["build", "library"]] if build else []
    up = base + ["--profile", "remote", "up", "-d", "--no-deps", *SERVICES]
    inject = ["--inject", f"TUNNEL_TOKEN={secret}"]
    if gateway_secret:
        inject += ["--inject", f"GATEWAY_API_KEY={gateway_secret}"]
    steps.append(["grant", "run", *inject, "--", *up])
    return steps


def _grant_secret_exists(name: str, env: dict[str, str]) -> bool:
    proc = _run(  # nosec B603 - fixed argv; grant show prints metadata, never the value
        ["grant", "show", name], capture_output=True, text=True, timeout=30, check=False, env=env
    )
    return proc.returncode == 0


def _check_grant_secrets(names: list[str], env: dict[str, str]) -> None:
    """Refuse, before compose runs, when a grant secret to inject does not exist."""
    if _which("grant") is None:
        raise CliError(EXIT_ENV_ERROR, "grant is not on PATH", "install grant")
    for name in names:
        if not _grant_secret_exists(name, env):
            hint = (
                "store the tunnel token with 'cultureflare --grant'"
                if name == DEFAULT_TUNNEL_SECRET
                else "add it with 'grant set NAME - --hidden', or pass --no-gateway-key to "
                "restart without embeddings"
            )
            raise CliError(EXIT_ENV_ERROR, f"grant secret {name} not found", hint)


def _step(argv: list[str], env: dict[str, str], *, verbose: bool) -> subprocess.CompletedProcess:
    tools = [argv[0]] + (["docker"] if argv[0] != "docker" and "docker" in argv else [])
    for tool in tools:
        if _which(tool) is None:
            raise CliError(EXIT_ENV_ERROR, f"{tool} is not on PATH", f"install {tool}")
    try:
        proc = _run(  # nosec B603 - argv is assembled from fixed literals and checked paths
            argv, capture_output=True, text=True, timeout=STEP_TIMEOUT, check=False, env=env
        )
    except subprocess.TimeoutExpired:
        raise CliError(
            EXIT_ENV_ERROR,
            f"{argv[0]} {argv[1]} timed out after {STEP_TIMEOUT}s",
            "check 'docker compose -f deploy/compose.yaml ps' and the library logs",
        ) from None
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        raise CliError(
            EXIT_ENV_ERROR,
            f"{argv[0]} {argv[1]} exited {proc.returncode}: {output[-800:]}",
            "read the output above; the previous containers keep running if 'up' failed",
        )
    if output and verbose:  # --json keeps stderr for the one JSON error
        emit_diagnostic(output)
    return proc


def _health(url: str, timeout: float) -> dict:
    """Poll *url* until it answers ``status: ok`` or *timeout* seconds pass."""
    deadline = time.monotonic() + timeout
    last = "no answer"
    while True:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:  # nosec B310 - fixed http URL
                body = json.loads(resp.read().decode("utf-8"))
            if isinstance(body, dict) and body.get("status") == "ok":
                return body
            last = str(body)[:200]
        except (OSError, ValueError) as exc:
            last = str(exc)
        if time.monotonic() >= deadline:
            raise CliError(
                EXIT_ENV_ERROR,
                f"{url} did not report healthy within {timeout:.0f}s ({last})",
                "check 'docker compose -f deploy/compose.yaml logs library'",
            )
        time.sleep(2)


def _health_url(values: dict[str, str]) -> str:
    """Where compose publishes the library: ``LIBRARY_PUBLISH_BIND``/``_PORT`` or loopback."""
    bind = values.get("LIBRARY_PUBLISH_BIND", "").strip()
    wildcard = ("", "0.0.0.0", "::")  # nosec B104 - a wildcard bind is reached via loopback
    host = "127.0.0.1" if bind in wildcard else bind
    if ":" in host:
        host = f"[{host}]"
    port = values.get("LIBRARY_PUBLISH_PORT") or DEFAULT_PORT
    # The library serves plain HTTP on its published port (TLS ends at the tunnel).
    return f"http://{host}:{port}/api/health"  # NOSONAR - local health probe, not a remote URL


def _inputs(args: argparse.Namespace) -> tuple[Path, Path]:
    """The env file and compose file, refused when missing; secret names refused when empty."""
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
    for flag, name in (
        ("--tunnel-secret", args.tunnel_secret),
        ("--gateway-secret", args.gateway_secret),
    ):
        if not (name or "").strip():
            raise CliError(
                EXIT_USER_ERROR, f"{flag} is empty", f"pass a grant secret name to {flag}"
            )
    return env_file, compose


def cmd_restart(args: argparse.Namespace) -> int:
    env_file, compose = _inputs(args)
    gateway = None if args.no_gateway_key else args.gateway_secret
    secrets = {"TUNNEL_TOKEN": args.tunnel_secret, "GATEWAY_API_KEY": args.gateway_secret}
    _refuse_secrets(env_file_keys(env_file), str(env_file), secrets)  # before value parsing
    values = read_env_file(env_file)
    check_settings(values, str(env_file))
    steps = plan(compose, env_file, args.tunnel_secret, build=args.build, gateway_secret=gateway)
    if args.dry_run:
        emit_result(
            (
                {"dry_run": True, "steps": steps}
                if _json(args)
                else "\n".join(shlex.join(s) for s in steps)
            ),
            json_mode=_json(args),
        )
        return 0
    env = _child_env(values)
    verbose = not _json(args)
    _check_grant_secrets([args.tunnel_secret, *([gateway] if gateway else [])], env)
    for argv in steps:
        _step(argv, env, verbose=verbose)
    health = _health(_health_url(values), args.timeout)
    probe = _step(
        _compose_base(compose, env_file)
        + ["exec", "-T", "library", "python3", "-c", _TARGETS_PROBE],
        env,
        verbose=False,
    )
    try:
        targets = json.loads(probe.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise CliError(
            EXIT_ENV_ERROR,
            f"the send-targets check printed no JSON: {probe.stdout[-300:]!r}",
            "run the check from docs/operations/deploy.md by hand",
        ) from None
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
                    f"--gateway-secret (grant secret injected as GATEWAY_API_KEY, default "
                    f"{DEFAULT_GATEWAY_SECRET}); --no-gateway-key skips it",
                    "needs grant and docker on PATH",
                ],
            },
        ],
        json_mode=_json(args),
    )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("deploy", help="Live stack (see 'culture-library deploy overview').")
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=cmd_overview, json=False)
    nsub = p.add_subparsers(dest="deploy_command", parser_class=type(p))
    rs = nsub.add_parser("restart", help="Recreate library + mcp from the host env file.")
    rs.add_argument("--env-file", help="Host env file (default: see 'deploy overview').")
    rs.add_argument("--compose", help="Compose file (default deploy/compose.yaml).")
    rs.add_argument(
        "--tunnel-secret", default=DEFAULT_TUNNEL_SECRET, help="grant secret holding the token."
    )
    gw = rs.add_mutually_exclusive_group()
    gw.add_argument(
        "--gateway-secret",
        default=DEFAULT_GATEWAY_SECRET,
        help="grant secret injected as GATEWAY_API_KEY (embeddings + reranker).",
    )
    gw.add_argument("--no-gateway-key", action="store_true", help="Do not inject GATEWAY_API_KEY.")
    rs.add_argument("--build", action="store_true", help="Rebuild the image first.")
    rs.add_argument("--dry-run", action="store_true", help="Print the commands; run nothing.")
    rs.add_argument(
        "--timeout", type=float, default=90.0, help="Seconds to wait for health (default 90)."
    )
    rs.add_argument("--json", action="store_true", help=_JSON_HELP)
    rs.set_defaults(func=cmd_restart)
    ov = nsub.add_parser("overview", help="Describe the deploy noun.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_overview)
