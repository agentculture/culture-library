"""``culture-library mirror`` CLI (plan task t14)."""

from __future__ import annotations

import json

import pytest

from culture_library.cli import main
from culture_library.cli._commands import learn
from culture_library.cli._commands import mirror as mirror_cmd
from culture_library.core import Store
from culture_library.core.mirror import FakeBackend, drain, enqueue, key_for
from culture_library.core.repodocs import RepoDocs
from culture_library.explain.catalog import ENTRIES

A = "a" * 40
REMOTE = "https://github.com/agentculture/devague"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("CULTURE_LIBRARY_S3_BUCKET", raising=False)


def _lib(tmp_path, n=2):
    s = Store(tmp_path / "data")
    for i in range(n):
        RepoDocs(s).push(
            remote=REMOTE,
            path=f"docs/specs/{i}.md",
            commit=A,
            committed_at="2026-10-01T00:00:00Z",
            default_branch_head=A,
            content=f"doc {i}\n".encode(),
        )
    return s


def test_overview_json(capsys):
    assert main(["mirror", "overview", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["subject"] == "culture-library mirror" and body["sections"]
    assert main(["mirror", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["subject"] == "culture-library mirror"


def test_status_reports_outbox(tmp_path, capsys):
    s = _lib(tmp_path, 0)
    with s.connect() as conn:
        enqueue(conn, "r/a.md@w", "0" * 64)
        enqueue(conn, "r/b.md@w", "1" * 64)
    be = FakeBackend()
    be.fail = ConnectionError("down")
    drain(s, be)
    assert main(["mirror", "status", "--data-dir", str(s.data_dir), "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["configured"] is False
    assert body["pending"] == 2 and body["done"] == 0
    assert body["oldest_pending_age_s"] >= 0
    assert body["last_error"]
    assert main(["mirror", "status", "--data-dir", str(s.data_dir)]) == 0
    assert "pending" in capsys.readouterr().out


def test_status_configured_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CULTURE_LIBRARY_S3_BUCKET", "lib-mirror")
    s = _lib(tmp_path, 0)
    assert main(["mirror", "status", "--data-dir", str(s.data_dir), "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["configured"] is True and body["bucket"] == "lib-mirror"
    assert body["pending"] == 0 and body["oldest_pending_age_s"] is None


def test_reconcile_without_backend_is_env_error(tmp_path, capsys):
    s = _lib(tmp_path)
    assert main(["mirror", "reconcile", "--data-dir", str(s.data_dir), "--json"]) == 2
    err = json.loads(capsys.readouterr().err)
    assert "CULTURE_LIBRARY_S3_BUCKET" in err["message"] + err["remediation"]


def test_reconcile_without_boto3_is_env_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CULTURE_LIBRARY_S3_BUCKET", "lib-mirror")

    def boom():
        raise ImportError("the S3 mirror backend needs boto3: install culture-library[s3]")

    monkeypatch.setattr(mirror_cmd, "_backend_from_env", boom)
    assert main(["mirror", "reconcile", "--data-dir", str(_lib(tmp_path).data_dir)]) == 2
    err = capsys.readouterr().err
    assert "boto3" in err and "hint:" in err


def test_reconcile_enqueues_missing_and_mismatched(tmp_path, monkeypatch, capsys):
    s = _lib(tmp_path, 3)
    be = FakeBackend()
    # core pushes write no outbox rows (the route does): an empty bucket misses all three.
    monkeypatch.setattr(mirror_cmd, "_backend_from_env", lambda: be)
    monkeypatch.setenv("CULTURE_LIBRARY_S3_BUCKET", "lib-mirror")
    assert main(["mirror", "reconcile", "--data-dir", str(s.data_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["enqueued"] == 3
    drain(s, be)
    assert len(be.objects) == 3
    # One missing, one with the wrong bytes.
    del be.objects[key_for(REMOTE_REPO(s), "docs/specs/0.md", A)]
    be.objects[key_for(REMOTE_REPO(s), "docs/specs/1.md", A)] = (b"stale", "text/markdown")
    assert main(["mirror", "reconcile", "--data-dir", str(s.data_dir), "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["enqueued"] == 2 and body["pending"] == 2
    assert main(["mirror", "reconcile", "--data-dir", str(s.data_dir)]) == 0
    assert "enqueued 2" in capsys.readouterr().out
    drain(s, be)
    assert main(["mirror", "reconcile", "--data-dir", str(s.data_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["enqueued"] == 0


def REMOTE_REPO(store):  # noqa: N802 - the repo name the push stored
    with store.reader() as conn:
        return conn.execute("SELECT repo FROM repodocs LIMIT 1").fetchone()[0]


def test_explain_and_learn_mention_mirror(capsys):
    for path in (
        ("mirror",),
        ("mirror", "overview"),
        ("mirror", "status"),
        ("mirror", "reconcile"),
    ):
        assert path in ENTRIES
        assert main(["explain", *path]) == 0
    capsys.readouterr()
    assert "mirror reconcile" in learn._TEXT
    paths = [c["path"] for c in learn._as_json_payload()["commands"]]
    assert ["mirror", "reconcile"] in paths and ["mirror", "status"] in paths
