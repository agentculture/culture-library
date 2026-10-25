"""Backup, verify and restore of the catalog (SQLite online backup + blob tree)."""

import json
import sqlite3

import pytest

from culture_library.core import Store, backup
from culture_library.core.repodocs import RepoDocs  # registers the repodoc tables


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
    assert second.blobs_copied == 1
    assert second.blobs_total == 4


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
    assert report.ok
    assert report.items == 3
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
    assert not report.ok
    assert any(sha in p for p in report.problems)


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
    assert "[Timer]" in out
    assert "OnCalendar=daily" in out
    assert "backup run" in out
    assert "culture-library backup run" in backup.schedule_text("/d", "/b", kind="cron")


# ------------------------------------------------------------------ repo-document blobs

_C1, _C2 = "a" * 40, "b" * 40


def _push_docs(store: Store):
    docs = RepoDocs(store)
    common = dict(remote="https://github.com/agentculture/x.git", committed_at="2026-01-01")
    a = docs.push(path="docs/specs/a.md", commit=_C1, default_branch_head=_C1,
                  content="# spec a", visibility="public", **common)  # fmt: skip
    b = docs.push(path="docs/plans/b.md", commit=_C2, default_branch_head=_C2,
                  content="# plan b", **common)  # fmt: skip
    return docs, a, b


def test_repodoc_blobs_are_backed_up_restored_and_counted(store, tmp_path):
    _seed(store)
    _docs, a, b = _push_docs(store)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    m = json.loads((snap.path / "manifest.json").read_text())
    assert m["repodoc_count"] == 2
    assert m["repodoc_version_count"] == 2
    assert {v["sha256"] for v in m["repodoc_versions"]} == {a.version.sha256, b.version.sha256}
    assert snap.blobs_total == 5
    assert snap.to_dict()["repodoc_versions"] == 2
    for sha in (a.version.sha256, b.version.sha256):
        assert (tmp_path / "bk" / "files" / sha[:2] / sha).is_file()
    assert backup.verify(snap.path).ok
    target = tmp_path / "restored"
    assert backup.restore(snap.path, target).ok
    restored = RepoDocs(Store(target))
    assert restored.content(a.doc.id, include_private=True) == b"# spec a"
    assert restored.content(b.doc.id, include_private=True) == b"# plan b"


def test_verify_detects_missing_repodoc_blob(store, tmp_path):
    _seed(store)
    _docs, a, _b = _push_docs(store)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    sha = a.version.sha256
    (tmp_path / "bk" / "files" / sha[:2] / sha).unlink()
    report = backup.verify(snap.path)
    assert not report.ok
    assert any(sha in p and "missing" in p for p in report.problems)
    with pytest.raises(backup.BackupError, match="verification"):
        backup.restore(snap.path, tmp_path / "restored")


def test_backup_refuses_when_a_repodoc_blob_is_missing_at_the_source(store, tmp_path):
    _docs, a, _b = _push_docs(store)
    store.blob_path(a.version.sha256).unlink()
    with pytest.raises(backup.BackupError, match="missing"):
        backup.run_backup(store.data_dir, tmp_path / "bk")


def test_replace_between_snapshot_and_blob_copy_retries_with_a_fresh_snapshot(
    tmp_path, monkeypatch
):
    """Lapse l1: a replace that commits after the DB snapshot unlinks a blob the snapshot
    still names. The backup takes a fresh snapshot instead of failing (or skipping it)."""
    from culture_library.core import backup, library
    from culture_library.core.store import Store

    store = Store(tmp_path / "data")
    item, _ = library.add(store, b"# v1\n\nfirst\n", "a.md")
    real = backup._backup_db
    fired = []

    def snapshot_then_replace(src, dst):
        real(src, dst)
        if not fired:  # the race: the file changes right after the snapshot
            fired.append(True)
            library.replace(store, item.id, b"# v2\n\nsecond\n", "a.md", include_private=True)

    monkeypatch.setattr(backup, "_backup_db", snapshot_then_replace)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    assert (
        snap.manifest["items"][0]["sha256"]
        == library.get(store, item.id, include_private=True).sha256
    )
    assert backup.verify(snap.path).ok
    assert len(backup.list_snapshots(tmp_path / "bk")) == 1


def test_blob_missing_while_still_referenced_still_fails(tmp_path):
    from culture_library.core import backup, library
    from culture_library.core.store import Store

    store = Store(tmp_path / "data")
    item, _ = library.add(store, b"# v1\n\nfirst\n", "a.md")
    store.blob_path(item.sha256).unlink()
    with pytest.raises(backup.BackupError, match="missing"):
        backup.run_backup(store.data_dir, tmp_path / "bk")


def test_blob_unlinked_during_copy_also_retries(tmp_path, monkeypatch):
    """The race can also land between the is_file() check and the copy itself."""
    from culture_library.core import backup, library
    from culture_library.core.store import Store

    store = Store(tmp_path / "data")
    item, _ = library.add(store, b"# v1\n\nfirst\n", "a.md")
    real_copy = backup.shutil.copyfile
    fired = []

    def racing_copy(src, dst, *a, **kw):
        if not fired:
            fired.append(True)
            library.replace(store, item.id, b"# v2\n\nsecond\n", "a.md", include_private=True)
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(backup.shutil, "copyfile", racing_copy)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    assert backup.verify(snap.path).ok
