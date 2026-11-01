"""``culture-library backup`` CLI."""

import json

from culture_library.cli import main
from culture_library.core import Store
from tests import backup_fixtures

# fixtures, picked up by pytest under these names
pg_bin = backup_fixtures.pg_bin
scratch_dsn = backup_fixtures.scratch_dsn


def _lib(tmp_path):
    s = Store(tmp_path / "data")
    s.add_item(b"one", kind="document", title="One", format="txt", visibility="public")
    return s


def test_run_verify_restore_roundtrip(tmp_path, capsys, scratch_dsn):
    s = _lib(tmp_path)
    bk = str(tmp_path / "bk")
    assert main(["backup", "run", "--data-dir", str(s.data_dir), "--to", bk, "--json"]) == 0
    snap = json.loads(capsys.readouterr().out)["snapshot"]
    assert main(["backup", "verify", snap, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    target = str(tmp_path / "r")
    assert (
        main(["backup", "restore", "--from", snap, "--to", target, "--dsn", scratch_dsn, "--json"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert Store(target, dsn=scratch_dsn).get_item(1).visibility == "public"


def test_run_without_destination_is_user_error(tmp_path, monkeypatch):
    monkeypatch.delenv("CULTURE_LIBRARY_BACKUP_DIR", raising=False)
    assert main(["backup", "run", "--data-dir", str(_lib(tmp_path).data_dir)]) == 1


def test_run_uses_env_dir(tmp_path, monkeypatch, capsys):
    s = _lib(tmp_path)
    monkeypatch.setenv("CULTURE_LIBRARY_BACKUP_DIR", str(tmp_path / "envbk"))
    assert main(["backup", "run", "--data-dir", str(s.data_dir)]) == 0
    assert (tmp_path / "envbk" / "snapshots").is_dir()


def test_restore_non_empty_is_user_error(tmp_path, capsys, scratch_dsn):
    s = _lib(tmp_path)
    bk = str(tmp_path / "bk")
    main(["backup", "run", "--data-dir", str(s.data_dir), "--to", bk, "--json"])
    snap = json.loads(capsys.readouterr().out)["snapshot"]
    assert (
        main(["backup", "restore", "--from", snap, "--to", str(s.data_dir), "--dsn", scratch_dsn])
        == 1
    )


def test_schedule_print_and_overview_explain(capsys):
    assert main(["backup", "schedule", "--print", "--to", "/mnt/bk"]) == 0
    assert "OnCalendar" in capsys.readouterr().out
    assert main(["backup", "schedule", "--print", "--kind", "cron", "--to", "/mnt/bk"]) == 0
    assert main(["backup", "overview"]) == 0
    assert main(["explain", "backup", "run"]) == 0
    assert "backup" in capsys.readouterr().out


def test_schedule_writes_user_units_and_overview_shows_last_success(tmp_path, capsys, monkeypatch):
    s = _lib(tmp_path)
    bk, units = str(tmp_path / "bk"), tmp_path / "units"
    assert main(["backup", "overview", "--to", bk]) == 0
    assert "none yet" in capsys.readouterr().out
    assert main(["backup", "run", "--data-dir", str(s.data_dir), "--to", bk]) == 0
    capsys.readouterr()
    assert main(["backup", "overview", "--to", bk, "--json"]) == 0
    assert "snapshots/" in capsys.readouterr().out
    argv = ["backup", "schedule", "--data-dir", str(s.data_dir), "--to", bk]
    command = f"/usr/local/bin/my-backup-wrapper {bk}"
    assert main([*argv, "--unit-dir", str(units), "--command", command]) == 0
    out = capsys.readouterr().out
    assert "systemctl --user enable --now culture-library-backup.timer" in out
    assert (units / "culture-library-backup.timer").is_file()
    assert (units / "culture-library-backup.service").is_file()


def test_missing_pg_tool_is_environment_error(tmp_path, monkeypatch, capsys):
    s = _lib(tmp_path)
    monkeypatch.setenv("CULTURE_LIBRARY_PG_BIN_DIR", str(tmp_path / "nothing"))
    code = main(["backup", "run", "--data-dir", str(s.data_dir), "--to", str(tmp_path / "bk")])
    assert code == 2
    assert "CULTURE_LIBRARY_PG_BIN_DIR" in capsys.readouterr().err


def test_schedule_refuses_to_write_units_without_a_command(tmp_path, capsys):
    """The default ExecStart (culture-library backup run + live.env) cannot reach the live
    database: its password is a grant secret, never in live.env, and postgres publishes no
    port. So writing units needs --command (the documented grant run ... wrapper)."""
    units = tmp_path / "units"
    argv = ["backup", "schedule", "--to", str(tmp_path / "bk"), "--unit-dir", str(units)]
    assert main(argv) == 1
    err = capsys.readouterr().err
    assert "--command" in err
    assert "hint:" in err
    assert "docs/operations/backup.md" in err
    assert not units.exists()


def test_schedule_print_uses_the_given_command(capsys):
    argv = ["backup", "schedule", "--print", "--to", "/mnt/bk", "--command", "/usr/bin/wrapper"]
    assert main(argv) == 0
    assert "ExecStart=/usr/bin/wrapper" in capsys.readouterr().out
    assert main([*argv, "--kind", "cron"]) == 0
    assert "/usr/bin/wrapper" in capsys.readouterr().out
