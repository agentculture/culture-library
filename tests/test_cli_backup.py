"""``culture-library backup`` CLI."""

import json

from culture_library.cli import main
from culture_library.core import Store


def _lib(tmp_path):
    s = Store(tmp_path / "data")
    s.add_item(b"one", kind="document", title="One", format="txt", visibility="public")
    return s


def test_run_verify_restore_roundtrip(tmp_path, capsys):
    s = _lib(tmp_path)
    bk = str(tmp_path / "bk")
    assert main(["backup", "run", "--data-dir", str(s.data_dir), "--to", bk, "--json"]) == 0
    snap = json.loads(capsys.readouterr().out)["snapshot"]
    assert main(["backup", "verify", snap, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    target = str(tmp_path / "r")
    assert main(["backup", "restore", "--from", snap, "--to", target, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert Store(target).get_item(1).visibility == "public"


def test_run_without_destination_is_user_error(tmp_path, monkeypatch):
    monkeypatch.delenv("CULTURE_LIBRARY_BACKUP_DIR", raising=False)
    assert main(["backup", "run", "--data-dir", str(_lib(tmp_path).data_dir)]) == 1


def test_run_uses_env_dir(tmp_path, monkeypatch, capsys):
    s = _lib(tmp_path)
    monkeypatch.setenv("CULTURE_LIBRARY_BACKUP_DIR", str(tmp_path / "envbk"))
    assert main(["backup", "run", "--data-dir", str(s.data_dir)]) == 0
    assert (tmp_path / "envbk" / "snapshots").is_dir()


def test_restore_non_empty_is_user_error(tmp_path, capsys):
    s = _lib(tmp_path)
    bk = str(tmp_path / "bk")
    main(["backup", "run", "--data-dir", str(s.data_dir), "--to", bk, "--json"])
    snap = json.loads(capsys.readouterr().out)["snapshot"]
    assert main(["backup", "restore", "--from", snap, "--to", str(s.data_dir)]) == 1


def test_schedule_print_and_overview_explain(capsys):
    assert main(["backup", "schedule", "--print", "--to", "/mnt/bk"]) == 0
    assert "OnCalendar" in capsys.readouterr().out
    assert main(["backup", "schedule", "--print", "--kind", "cron", "--to", "/mnt/bk"]) == 0
    assert main(["backup", "overview"]) == 0
    assert main(["explain", "backup", "run"]) == 0
    assert "backup" in capsys.readouterr().out
