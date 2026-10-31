"""``culture-library deploy`` — recreate the live stack's library and mcp containers.

``deploy restart`` is the CLI seam for the runbook's "Restart the live stack"
(``docs/operations/deploy.md``): it checks the host env file first (every required
setting present, no placeholder, send targets that parse), then runs, with a fixed argv
and no shell::

    grant run --inject TUNNEL_TOKEN=<secret> --inject GATEWAY_API_KEY=<secret> -- \\
      docker compose -f deploy/compose.yaml --env-file <env file> --profile remote \\
      up -d --no-deps library mcp

When the env file names an S3 bucket (``CULTURE_LIBRARY_S3_BUCKET``) it also injects
``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` (and ``AWS_SESSION_TOKEN`` if asked) from
grant; those never enter the env file either.

``deploy check-bucket`` audits the mirror bucket's safety settings (see ``cmd_check_bucket``).

It checks ``/api/health`` and the send targets inside the new container. ``--build``
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
# The grant secrets holding the S3 mirror's AWS credentials (names only).
DEFAULT_AWS_KEY_ID_SECRET = "CULTURE_LIBRARY_AWS_ACCESS_KEY_ID"  # nosec B105
DEFAULT_AWS_SECRET_KEY_SECRET = "CULTURE_LIBRARY_AWS_SECRET_ACCESS_KEY"  # nosec B105
S3_BUCKET_ENV = "CULTURE_LIBRARY_S3_BUCKET"
# Optional S3 settings: empty/absent means "off"/default, but a placeholder is refused.
S3_SETTINGS = (S3_BUCKET_ENV, "CULTURE_LIBRARY_S3_REGION", "CULTURE_LIBRARY_S3_PREFIX")
AWS_SESSION_ENV = "AWS_SESSION_TOKEN"
SECRET_SETTINGS = {
    "TUNNEL_TOKEN": DEFAULT_TUNNEL_SECRET,
    "GATEWAY_API_KEY": DEFAULT_GATEWAY_SECRET,
    "AWS_ACCESS_KEY_ID": DEFAULT_AWS_KEY_ID_SECRET,
    "AWS_SECRET_ACCESS_KEY": DEFAULT_AWS_SECRET_KEY_SECRET,
    AWS_SESSION_ENV: "(an AWS session-token secret via --aws-session-secret)",
}
PAB_FLAGS = ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")
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
    _check_required(values, source)
    for key in S3_SETTINGS:
        if _PLACEHOLDER.search(values.get(key, "")):
            raise _placeholder(source, key, values[key])
    _check_targets(values, source)


def _check_required(values: dict[str, str], source: str) -> None:
    for key in REQUIRED_SETTINGS:
        value = values.get(key, "")
        if not value.strip():
            raise CliError(
                EXIT_USER_ERROR, f"{source}: {key} is missing or empty", f"set it; see {_RUNBOOK}"
            )
        if key != send.TARGETS_ENV and _PLACEHOLDER.search(value):
            raise _placeholder(source, key, value)


def _check_targets(values: dict[str, str], source: str) -> None:
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
    aws_secrets: dict[str, str] | None = None,
) -> list[list[str]]:
    """The commands ``restart`` runs, in order. Secrets are injected by name from grant."""
    base = _compose_base(compose, env_file)
    steps = [base + ["build", "library"]] if build else []
    up = base + ["--profile", "remote", "up", "-d", "--no-deps", *SERVICES]
    inject = ["--inject", f"TUNNEL_TOKEN={secret}"]
    if gateway_secret:
        inject += ["--inject", f"GATEWAY_API_KEY={gateway_secret}"]
    for var, name in (aws_secrets or {}).items():
        inject += ["--inject", f"{var}={name}"]
    steps.append(["grant", "run", *inject, "--", *up])
    return steps


def _grant_secret_exists(name: str, env: dict[str, str]) -> bool:
    proc = _run(  # nosec B603 - fixed argv; grant show prints metadata, never the value
        ["grant", "show", name], capture_output=True, text=True, timeout=30, check=False, env=env
    )
    return proc.returncode == 0


def _check_grant_secrets(
    names: list[str], env: dict[str, str], aws_names: list[str] | None = None
) -> None:
    """Refuse, before compose runs, when a grant secret to inject does not exist."""
    if _which("grant") is None:
        raise CliError(EXIT_ENV_ERROR, "grant is not on PATH", "install grant")
    for name in names:
        if not _grant_secret_exists(name, env):
            if name == DEFAULT_TUNNEL_SECRET:
                hint = "store the tunnel token with 'cultureflare --grant'"
            elif name in (DEFAULT_AWS_KEY_ID_SECRET, DEFAULT_AWS_SECRET_KEY_SECRET) or (
                aws_names and name in aws_names
            ):
                hint = (
                    "add it with 'grant set NAME - --hidden', or pass --no-aws-credentials "
                    "to restart without the S3 mirror credentials"
                )
            else:
                hint = (
                    "add it with 'grant set NAME - --hidden', or pass --no-gateway-key to "
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
        ("--aws-key-id-secret", args.aws_key_id_secret),
        ("--aws-secret-key-secret", args.aws_secret_key_secret),
    ):
        if not (name or "").strip():
            raise CliError(
                EXIT_USER_ERROR, f"{flag} is empty", f"pass a grant secret name to {flag}"
            )
    return env_file, compose


def cmd_restart(args: argparse.Namespace) -> int:
    env_file, compose = _inputs(args)
    gateway = None if args.no_gateway_key else args.gateway_secret
    secrets = {
        **SECRET_SETTINGS,
        "TUNNEL_TOKEN": args.tunnel_secret,
        "GATEWAY_API_KEY": args.gateway_secret,
        "AWS_ACCESS_KEY_ID": args.aws_key_id_secret,
        "AWS_SECRET_ACCESS_KEY": args.aws_secret_key_secret,
    }
    _refuse_secrets(env_file_keys(env_file), str(env_file), secrets)  # before value parsing
    values = read_env_file(env_file)
    check_settings(values, str(env_file))
    aws: dict[str, str] = {}
    if values.get(S3_BUCKET_ENV, "").strip() and not args.no_aws_credentials:
        aws = {
            "AWS_ACCESS_KEY_ID": args.aws_key_id_secret,
            "AWS_SECRET_ACCESS_KEY": args.aws_secret_key_secret,
        }
        if args.aws_session_secret:
            aws[AWS_SESSION_ENV] = args.aws_session_secret
    steps = plan(
        compose,
        env_file,
        args.tunnel_secret,
        build=args.build,
        gateway_secret=gateway,
        aws_secrets=aws,
    )
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
    _check_grant_secrets(
        [args.tunnel_secret, *([gateway] if gateway else []), *aws.values()],
        env,
        aws_names=list(aws.values()),
    )
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


def _aws_client(service: str, region: str | None):
    """A boto3 client, imported lazily (the ``[s3]`` extra); a test seam."""
    try:
        import boto3  # noqa: PLC0415
    except ImportError:
        raise CliError(
            EXIT_ENV_ERROR,
            "deploy check-bucket needs boto3",
            "install it: pip install 'culture-library[s3]'",
        ) from None
    return boto3.client(service, region_name=region) if region else boto3.client(service)


def _code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def _chk(cid: str, status: str, message: str, remediation: str = "") -> dict:
    return {
        "id": cid,
        "passed": status == "pass",
        "status": status,
        "severity": "error",
        "message": message,
        "remediation": remediation,
    }


def _check_pab(s3, bucket: str) -> dict:
    cid = "public_access_block"
    fix = (
        f"aws s3api put-public-access-block --bucket {bucket} --public-access-block-configuration "
    )
    fix += ",".join(f"{f}=true" for f in PAB_FLAGS)
    try:
        cfg = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
    # any client error is reported, not raised
    except Exception as exc:  # noqa: BLE001
        if _code(exc) == "NoSuchPublicAccessBlockConfiguration":
            return _chk(cid, "fail", "Block Public Access is not configured", fix)
        return _chk(
            cid,
            "unknown",
            f"cannot read Block Public Access ({_code(exc) or exc})",
            "grant s3:GetBucketPublicAccessBlock to the checking identity",
        )
    off = [f for f in PAB_FLAGS if not cfg.get(f)]
    if off:
        return _chk(cid, "fail", f"Block Public Access flags off: {', '.join(off)}", fix)
    return _chk(cid, "pass", "all four Block Public Access flags are on")


def _check_encryption(s3, bucket: str) -> dict:
    cid = "default_encryption"
    fix = "enable default SSE: aws s3api put-bucket-encryption (AES256 or aws:kms)"
    try:
        cfg = s3.get_bucket_encryption(Bucket=bucket)["ServerSideEncryptionConfiguration"]
    except Exception as exc:  # noqa: BLE001
        if _code(exc) == "ServerSideEncryptionConfigurationNotFoundError":
            return _chk(cid, "fail", "no default encryption rule", fix)
        return _chk(
            cid,
            "unknown",
            f"cannot read bucket encryption ({_code(exc) or exc})",
            "grant s3:GetEncryptionConfiguration to the checking identity",
        )
    if not cfg.get("Rules"):
        return _chk(cid, "fail", "no default encryption rule", fix)
    return _chk(cid, "pass", "a default encryption rule is present")


def _check_versioning(s3, bucket: str) -> dict:
    cid = "versioning"
    try:
        status = s3.get_bucket_versioning(Bucket=bucket).get("Status")
    except Exception as exc:  # noqa: BLE001
        return _chk(
            cid,
            "unknown",
            f"cannot read bucket versioning ({_code(exc) or exc})",
            "grant s3:GetBucketVersioning to the checking identity",
        )
    if status != "Enabled":
        return _chk(
            cid,
            "fail",
            f"versioning is {status or 'not enabled'}",
            f"aws s3api put-bucket-versioning --bucket {bucket} "
            "--versioning-configuration Status=Enabled",
        )
    return _chk(cid, "pass", "versioning is Enabled")


def _policy_source(arn: str) -> str:
    """The IAM principal to simulate: an assumed-role session maps to its role."""
    parts = arn.split(":", 5)
    if len(parts) == 6 and parts[2] == "sts" and parts[5].startswith("assumed-role/"):
        role = parts[5].split("/")[1]
        return f"arn:{parts[1]}:iam::{parts[4]}:role/{role}"
    return arn


def _check_delete(sts, iam, bucket: str) -> dict:
    cid = "delete_object_denied"
    hint = (
        "grant iam:SimulatePrincipalPolicy to the checking identity (or check by hand that "
        "the library's credentials cannot s3:DeleteObject)"
    )
    try:
        arn = sts.get_caller_identity()["Arn"]
        result = iam.simulate_principal_policy(
            PolicySourceArn=_policy_source(arn),
            ActionNames=["s3:DeleteObject"],
            ResourceArns=[f"arn:aws:s3:::{bucket}/*"],
        )
        decisions = [r["EvalDecision"] for r in result["EvaluationResults"]]
    except Exception as exc:  # noqa: BLE001
        return _chk(
            cid, "unknown", f"could not simulate s3:DeleteObject ({_code(exc) or exc})", hint
        )
    if "allowed" in decisions:
        return _chk(
            cid,
            "fail",
            f"{arn} may call s3:DeleteObject on the bucket",
            "remove s3:DeleteObject (and s3:DeleteObjectVersion) from its IAM policy",
        )
    return _chk(cid, "pass", f"s3:DeleteObject is denied for {arn} ({', '.join(decisions)})")


def cmd_check_bucket(args: argparse.Namespace) -> int:
    bucket = (args.bucket or os.environ.get(S3_BUCKET_ENV, "")).strip()
    if not bucket:
        raise CliError(
            EXIT_USER_ERROR,
            "no bucket to check",
            f"pass --bucket or set {S3_BUCKET_ENV}",
        )
    region = args.region or os.environ.get("CULTURE_LIBRARY_S3_REGION") or None
    s3 = _aws_client("s3", region)
    checks = [
        _check_pab(s3, bucket),
        _check_encryption(s3, bucket),
        _check_versioning(s3, bucket),
        _check_delete(_aws_client("sts", region), _aws_client("iam", region), bucket),
    ]
    report = {"healthy": all(c["passed"] for c in checks), "bucket": bucket, "checks": checks}
    if _json(args):
        emit_result(report, json_mode=True)
    else:
        lines = [f"bucket {bucket}: {'healthy' if report['healthy'] else 'unhealthy'}"]
        for c in checks:
            lines.append(f"  [{c['status']}] {c['id']}: {c['message']}")
            if not c["passed"]:
                lines.append(f"      fix: {c['remediation']}")
        emit_result("\n".join(lines), json_mode=False)
    return 0 if report["healthy"] else 1


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "culture-library deploy",
        [
            {
                "title": "Verbs",
                "items": [
                    "restart — recreate library + mcp from the host env file, then check them",
                    "check-bucket — audit the S3 mirror bucket (public access block, default "
                    "encryption, versioning, no DeleteObject for the credentials)",
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
                    f"--aws-key-id-secret / --aws-secret-key-secret (grant secrets injected as "
                    f"AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY when "
                    f"{S3_BUCKET_ENV} is set; defaults {DEFAULT_AWS_KEY_ID_SECRET}, "
                    f"{DEFAULT_AWS_SECRET_KEY_SECRET}); --no-aws-credentials skips them",
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
    rs.add_argument(
        "--aws-key-id-secret",
        default=DEFAULT_AWS_KEY_ID_SECRET,
        help="grant secret injected as AWS_ACCESS_KEY_ID (only when an S3 bucket is set).",
    )
    rs.add_argument(
        "--aws-secret-key-secret",
        default=DEFAULT_AWS_SECRET_KEY_SECRET,
        help="grant secret injected as AWS_SECRET_ACCESS_KEY.",
    )
    rs.add_argument(
        "--aws-session-secret",
        default=None,
        help="optional grant secret injected as AWS_SESSION_TOKEN.",
    )
    rs.add_argument(
        "--no-aws-credentials", action="store_true", help="Do not inject AWS credentials."
    )
    rs.add_argument("--build", action="store_true", help="Rebuild the image first.")
    rs.add_argument("--dry-run", action="store_true", help="Print the commands; run nothing.")
    rs.add_argument(
        "--timeout", type=float, default=90.0, help="Seconds to wait for health (default 90)."
    )
    rs.add_argument("--json", action="store_true", help=_JSON_HELP)
    rs.set_defaults(func=cmd_restart)
    cb = nsub.add_parser("check-bucket", help="Audit the S3 mirror bucket's safety settings.")
    cb.add_argument("--bucket", help=f"Bucket (default ${S3_BUCKET_ENV}).")
    cb.add_argument("--region", help="AWS region (default $CULTURE_LIBRARY_S3_REGION).")
    cb.add_argument("--json", action="store_true", help=_JSON_HELP)
    cb.set_defaults(func=cmd_check_bucket)
    ov = nsub.add_parser("overview", help="Describe the deploy noun.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_overview)
