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
    assert calls[1][:2] == ["docker", "compose"] and "exec" in calls[1]
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
