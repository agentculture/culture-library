"""Backup, verify and restore of the catalog (SQLite online backup + blob tree)."""

import json
import sqlite3

import pytest

from culture_library.core import Store, backup


def _seed(store: Store) -> None:
    store.add_item(b"alpha", kind="document", title="A", format="txt", visibility="public")
    store.add_item(b"beta", kind="book", title="B", format="epub")
    store.add_item(b"gamma", kind="document", title="C", format="txt", visibility="public")


def test_run_creates_snapshot_with_manifest(store, tmp_path):
    _seed(store)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    m = json.loads((snap.path / "manifest.json").read_text())
    assert m["item_count"] == 3
    assert [i["id"] for i in m["items"]] == [1, 2, 3]
    assert m["visibility_counts"] == {"public": 2, "private": 1}
    assert (snap.path / "library.db").is_file()
    for i in m["items"]:
        assert (tmp_path / "bk" / "files" / i["sha256"][:2] / i["sha256"]).is_file()
    # the snapshot is a consistent, openable database
    n = sqlite3.connect(snap.path / "library.db").execute("SELECT count(*) FROM items")
    assert n.fetchone()[0] == 3


def test_blobs_are_incremental(store, tmp_path):
    _seed(store)
    first = backup.run_backup(store.data_dir, tmp_path / "bk")
    assert first.blobs_copied == 3
    store.add_item(b"delta", kind="document", title="D", format="txt")
    second = backup.run_backup(store.data_dir, tmp_path / "bk")
    assert second.blobs_copied == 1 and second.blobs_total == 4


def test_retention_keeps_last_n(store, tmp_path):
    _seed(store)
    for _ in range(4):
        backup.run_backup(store.data_dir, tmp_path / "bk", keep=2)
    assert len(backup.list_snapshots(tmp_path / "bk")) == 2


def test_restore_reproduces_ids_sha_visibility(store, tmp_path):
    _seed(store)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    target = tmp_path / "restored"
    report = backup.restore(snap.path, target)
    assert report.ok and report.items == 3
    original = {(i.id, i.sha256, i.visibility) for i in _items(store)}
    assert {(i.id, i.sha256, i.visibility) for i in _items(Store(target))} == original
    assert Store(target).read_bytes(2) == b"beta"


def _items(s: Store):
    return [s.get_item(i) for i in (1, 2, 3)]


def test_restore_refuses_non_empty_target(store, tmp_path):
    _seed(store)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    target = tmp_path / "t"
    target.mkdir()
    (target / "x").write_text("x")
    with pytest.raises(backup.BackupError, match="not empty"):
        backup.restore(snap.path, target)


def test_verify_detects_corrupt_blob(store, tmp_path):
    _seed(store)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    sha = json.loads((snap.path / "manifest.json").read_text())["items"][0]["sha256"]
    (tmp_path / "bk" / "files" / sha[:2] / sha).write_bytes(b"tampered")
    report = backup.verify(snap.path)
    assert not report.ok and any(sha in p for p in report.problems)


def test_snapshot_taken_while_writing(store, tmp_path):
    _seed(store)
    writer = sqlite3.connect(store.db_path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")  # open write txn: WAL readers/backup still work
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    writer.execute("ROLLBACK")
    assert backup.verify(snap.path).ok


def test_missing_source_is_error(tmp_path):
    with pytest.raises(backup.BackupError):
        backup.run_backup(tmp_path / "nope", tmp_path / "bk")


def test_schedule_text():
    out = backup.schedule_text("/srv/lib", "/mnt/bk", kind="systemd")
    assert "[Timer]" in out and "OnCalendar=daily" in out and "backup run" in out
    assert "culture-library backup run" in backup.schedule_text("/d", "/b", kind="cron")
