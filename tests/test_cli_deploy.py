"""``culture-library deploy restart``: recreate library + mcp from the host env file."""

import json
import subprocess

import pytest

from culture_library.cli import main
from culture_library.cli._commands import deploy

TARGETS = '[{"label":"x3","type":"reader","enabled":true}]'


def _env(tmp_path, **over):
    values = {
        "COMPOSE_PROJECT_NAME": "culture-library",
        "CULTURE_LIBRARY_ACCESS_TEAM_DOMAIN": "team.cloudflareaccess.com",
        "CULTURE_LIBRARY_ACCESS_AUD": "aud123",
        "CULTURE_LIBRARY_SEND_TARGETS": f"'{TARGETS}'",
    }
    values.update(over)
    path = tmp_path / "live.env"
    path.write_text(
        "# live settings\n" + "".join(f"{k}={v}\n" for k, v in values.items() if v is not None)
    )
    return path


@pytest.fixture
def compose(tmp_path):
    path = tmp_path / "compose.yaml"
    path.write_text("services: {}\n")
    return path


@pytest.fixture
def calls(monkeypatch):
    """Record every subprocess argv; the targets probe answers with the labels."""
    seen: list[list[str]] = []

    def fake_run(argv, **kw):
        seen.append(list(argv))
        out = '["x3"]\n' if "exec" in argv else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(deploy, "_run", fake_run)
    monkeypatch.setattr(deploy, "_which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(deploy, "_health", lambda url, timeout: {"status": "ok", "version": "x"})
    return seen


def _restart(*extra):
    return main(["deploy", "restart", *extra, "--json"])


def test_dry_run_prints_the_exact_command_and_runs_nothing(tmp_path, compose, calls, capsys):
    env = _env(tmp_path)
    assert _restart("--env-file", str(env), "--compose", str(compose), "--dry-run") == 0
    out = json.loads(capsys.readouterr().out)
    assert calls == []
    assert out["dry_run"] is True
    assert out["steps"] == [
        [
            "grant",
            "run",
            "--inject",
            "TUNNEL_TOKEN=CULTUREFLARE_LIBRARY_CULTURE_DEV_TUNNEL_TOKEN",
            "--",
            "docker",
            "compose",
            "-f",
            str(compose),
            "--env-file",
            str(env),
            "--profile",
            "remote",
            "up",
            "-d",
            "--no-deps",
            "library",
            "mcp",
        ]
    ]


def test_build_adds_an_image_build_first(tmp_path, compose, calls, capsys):
    env = _env(tmp_path)
    assert _restart("--env-file", str(env), "--compose", str(compose), "--build", "--dry-run") == 0
    steps = json.loads(capsys.readouterr().out)["steps"]
    assert steps[0] == [
        "docker",
        "compose",
        "-f",
        str(compose),
        "--env-file",
        str(env),
        "build",
        "library",
    ]
    assert steps[1][0] == "grant"


def test_restart_runs_then_checks_health_and_send_targets(tmp_path, compose, calls, capsys):
    env = _env(tmp_path)
    assert _restart("--env-file", str(env), "--compose", str(compose)) == 0
    out = json.loads(capsys.readouterr().out)
    assert calls[0][0] == "grant"
    assert calls[1][:2] == ["docker", "compose"]
    assert "exec" in calls[1]
    assert out["health"]["status"] == "ok"
    assert out["send_targets"] == ["x3"]


@pytest.mark.parametrize(
    "over, needle",
    [
        ({"CULTURE_LIBRARY_SEND_TARGETS": "'[...]'"}, "CULTURE_LIBRARY_SEND_TARGETS"),
        ({"CULTURE_LIBRARY_SEND_TARGETS": '\'[{"type":"x"}]\''}, "label"),
        ({"CULTURE_LIBRARY_ACCESS_AUD": None}, "CULTURE_LIBRARY_ACCESS_AUD"),
        ({"CULTURE_LIBRARY_ACCESS_TEAM_DOMAIN": ""}, "CULTURE_LIBRARY_ACCESS_TEAM_DOMAIN"),
        ({"COMPOSE_PROJECT_NAME": "<project>"}, "COMPOSE_PROJECT_NAME"),
    ],
)
def test_bad_env_file_is_refused_before_anything_runs(
    tmp_path, compose, calls, capsys, over, needle
):
    env = _env(tmp_path, **over)
    assert _restart("--env-file", str(env), "--compose", str(compose)) == 1
    err = json.loads(capsys.readouterr().err)
    assert needle in err["message"]
    assert calls == []


def test_missing_env_file_is_user_error(tmp_path, compose, calls, capsys):
    assert _restart("--env-file", str(tmp_path / "nope.env"), "--compose", str(compose)) == 1
    assert "remediation" in json.loads(capsys.readouterr().err)
    assert calls == []


def test_env_file_defaults_to_the_env_var(tmp_path, compose, calls, capsys, monkeypatch):
    env = _env(tmp_path)
    monkeypatch.setenv("CULTURE_LIBRARY_LIVE_ENV", str(env))
    assert _restart("--compose", str(compose), "--dry-run") == 0
    assert str(env) in json.loads(capsys.readouterr().out)["steps"][0]


def test_failed_step_is_environment_error(tmp_path, compose, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "_which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(
        deploy,
        "_run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom"),
    )
    assert _restart("--env-file", str(_env(tmp_path)), "--compose", str(compose)) == 2
    assert "boom" in json.loads(capsys.readouterr().err)["message"]


def test_missing_tool_is_environment_error(tmp_path, compose, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "_which", lambda tool: None)
    assert _restart("--env-file", str(_env(tmp_path)), "--compose", str(compose)) == 2
    assert "grant" in json.loads(capsys.readouterr().err)["message"]


def test_overview_and_explain(capsys):
    assert main(["deploy", "overview", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["subject"] == "culture-library deploy"
    assert main(["explain", "deploy", "restart"]) == 0
    assert "--env-file" in capsys.readouterr().out


def test_shell_values_cannot_override_the_checked_file(tmp_path, compose, monkeypatch, capsys):
    seen_env = []

    def fake_run(argv, **kw):
        seen_env.append(kw.get("env"))
        out = '["x3"]\n' if "exec" in argv else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(deploy, "_run", fake_run)
    monkeypatch.setattr(deploy, "_which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(deploy, "_health", lambda url, timeout: {"status": "ok"})
    monkeypatch.setenv("CULTURE_LIBRARY_SEND_TARGETS", "[...]")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "other")
    monkeypatch.setenv("KEEP_ME", "1")
    assert _restart("--env-file", str(_env(tmp_path)), "--compose", str(compose)) == 0
    for env in seen_env:
        assert env is not None
        assert "CULTURE_LIBRARY_SEND_TARGETS" not in env
        assert "COMPOSE_PROJECT_NAME" not in env
        assert env["KEEP_ME"] == "1"


def test_placeholder_target_label_is_refused(tmp_path, compose, calls, capsys):
    bad = '\'[{"label":"<label>","type":"<description>","enabled":true}]\''
    env = _env(tmp_path, CULTURE_LIBRARY_SEND_TARGETS=bad)
    assert _restart("--env-file", str(env), "--compose", str(compose)) == 1
    assert "placeholder" in json.loads(capsys.readouterr().err)["message"]
    assert calls == []


def test_env_file_reads_like_compose(tmp_path):
    path = tmp_path / "e.env"
    path.write_text(
        "export COMPOSE_PROJECT_NAME=culture-library # live\n"
        f"CULTURE_LIBRARY_SEND_TARGETS='{TARGETS}'  # x3 reader\n"
        'CULTURE_LIBRARY_ACCESS_AUD="aud#1"\n'
        "CULTURE_LIBRARY_ACCESS_TEAM_DOMAIN=team # comment\n"
    )
    values = deploy.read_env_file(path)
    assert values["COMPOSE_PROJECT_NAME"] == "culture-library"
    assert values["CULTURE_LIBRARY_SEND_TARGETS"] == TARGETS
    assert values["CULTURE_LIBRARY_ACCESS_AUD"] == "aud#1"
    assert values["CULTURE_LIBRARY_ACCESS_TEAM_DOMAIN"] == "team"


def test_interpolated_value_is_refused(tmp_path, compose, calls, capsys):
    env = _env(tmp_path, CULTURE_LIBRARY_ACCESS_AUD="${AUD}")
    assert _restart("--env-file", str(env), "--compose", str(compose)) == 1
    assert "CULTURE_LIBRARY_ACCESS_AUD" in json.loads(capsys.readouterr().err)["message"]


@pytest.mark.parametrize(
    "bind, host", [(None, "127.0.0.1"), ("0.0.0.0", "127.0.0.1"), ("192.168.1.10", "192.168.1.10")]
)
def test_health_url_follows_the_publish_bind(tmp_path, compose, monkeypatch, bind, host):
    urls = []
    monkeypatch.setattr(deploy, "_which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(
        deploy,
        "_run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 0, stdout='["x3"]\n' if "exec" in argv else "", stderr=""
        ),
    )
    monkeypatch.setattr(
        deploy, "_health", lambda url, timeout: urls.append(url) or {"status": "ok"}
    )
    env = _env(tmp_path, LIBRARY_PUBLISH_BIND=bind, LIBRARY_PUBLISH_PORT="9000")
    assert _restart("--env-file", str(env), "--compose", str(compose)) == 0
    assert urls == [f"http://{host}:9000/api/health"]


def test_step_timeout_is_environment_error(tmp_path, compose, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "_which", lambda tool: f"/usr/bin/{tool}")

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

    monkeypatch.setattr(deploy, "_run", slow)
    assert _restart("--env-file", str(_env(tmp_path)), "--compose", str(compose)) == 2
    assert "timed out" in json.loads(capsys.readouterr().err)["message"]


def test_json_mode_keeps_stderr_parseable(tmp_path, compose, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "_which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(
        deploy,
        "_run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="", stderr="progress\n"),
    )

    def down(url, timeout):
        raise deploy.CliError(2, "not healthy", "check logs")

    monkeypatch.setattr(deploy, "_health", down)
    assert _restart("--env-file", str(_env(tmp_path)), "--compose", str(compose)) == 2
    assert json.loads(capsys.readouterr().err)["message"] == "not healthy"


def test_health_retries_on_a_non_object_body(monkeypatch):
    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    bodies = [b"[]", b'{"status": "ok"}']
    monkeypatch.setattr(deploy.urllib.request, "urlopen", lambda url, timeout: Resp(bodies.pop(0)))
    monkeypatch.setattr(deploy.time, "sleep", lambda s: None)
    assert deploy._health("http://127.0.0.1:1/api/health", 30) == {"status": "ok"}


def test_docker_is_checked_on_path_even_behind_grant(tmp_path, compose, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "_which", lambda tool: None if tool == "docker" else "/bin/x")
    assert _restart("--env-file", str(_env(tmp_path)), "--compose", str(compose)) == 2
    assert "docker" in json.loads(capsys.readouterr().err)["message"]


def test_text_dry_run_is_shell_quoted(tmp_path, calls, capsys):
    spaced = tmp_path / "my dir"
    spaced.mkdir()
    compose = spaced / "compose.yaml"
    compose.write_text("services: {}\n")
    env = _env(spaced)
    assert (
        main(["deploy", "restart", "--env-file", str(env), "--compose", str(compose), "--dry-run"])
        == 0
    )
    assert f"'{env}'" in capsys.readouterr().out
