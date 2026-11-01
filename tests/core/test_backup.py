"""Backup, verify, restore and schedule of the library on Postgres (pg_dump / pg_restore).

The client tools come from PATH (CI installs postgresql-client-17). On a host without them the
``pg_bin`` fixture builds wrappers that run the tools inside the ``culture-library-pgtest``
container (see docs/operations/testing.md); with neither, the tests fail, never skip.
"""

import json

import pytest

from culture_library.core import Store, backup
from tests import backup_fixtures

# fixtures, picked up by pytest under these names
pg_bin = backup_fixtures.pg_bin
scratch_dsn = backup_fixtures.scratch_dsn


def _seed(store: Store) -> None:
    store.add_item(b"alpha", kind="document", title="A", format="txt", visibility="public")
    store.add_item(b"beta", kind="book", title="B", format="epub")
    store.add_item(b"gamma", kind="document", title="C", format="txt", visibility="public")


def test_run_creates_snapshot_with_dump_and_manifest(store, tmp_path):
    _seed(store)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    m = json.loads((snap.path / "manifest.json").read_text())
    assert m["item_count"] == 3
    assert [i["id"] for i in m["items"]] == [1, 2, 3]
    assert m["visibility_counts"] == {"public": 2, "private": 1}
    assert m["tables"]["items"] == 3
    assert "items" in m["tables"]
    assert "schema_version" in m["tables"]
    assert (snap.path / "library.dump").read_bytes()[:5] == b"PGDMP"  # custom format
    for i in m["items"]:
        assert (tmp_path / "bk" / "files" / i["sha256"][:2] / i["sha256"]).is_file()
    assert backup.verify(snap.path).ok


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


def test_restore_into_empty_database_reproduces_every_table_count(store, tmp_path, scratch_dsn):
    _seed(store)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    target = tmp_path / "restored"
    report = backup.restore(snap.path, target, dsn=scratch_dsn)
    assert report.ok, report.problems
    assert report.items == 3
    manifest = json.loads((snap.path / "manifest.json").read_text())
    restored = Store(target, dsn=scratch_dsn)  # opening a restored database runs no migration
    with restored.reader() as conn:
        counts = backup._table_counts(conn)
    assert counts == manifest["tables"]
    assert counts["items"] == 3
    assert restored.read_bytes(2) == b"beta"
    original = {(i.id, i.sha256, i.visibility) for i in (store.get_item(n) for n in (1, 2, 3))}
    assert {(i.id, i.sha256, i.visibility) for i in (restored.get_item(n) for n in (1, 2, 3))} == (
        original
    )
    for i in manifest["items"]:
        blob = target / "files" / i["sha256"][:2] / i["sha256"]
        assert backup._sha256_file(blob) == i["sha256"]


def test_restore_refuses_non_empty_files_target(store, tmp_path, scratch_dsn):
    _seed(store)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    target = tmp_path / "t"
    target.mkdir()
    (target / "x").write_text("x")
    with pytest.raises(backup.BackupError, match="not empty"):
        backup.restore(snap.path, target, dsn=scratch_dsn)


def test_restore_refuses_non_empty_database(store, tmp_path, pg_dsn):
    _seed(store)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    with pytest.raises(backup.BackupError, match="database is not empty"):
        backup.restore(snap.path, tmp_path / "restored", dsn=pg_dsn)
    assert not (tmp_path / "restored").exists()


def test_verify_detects_corrupt_blob(store, tmp_path):
    _seed(store)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    sha = json.loads((snap.path / "manifest.json").read_text())["items"][0]["sha256"]
    (tmp_path / "bk" / "files" / sha[:2] / sha).write_bytes(b"tampered")
    report = backup.verify(snap.path)
    assert not report.ok
    assert any(sha in p for p in report.problems)


def test_verify_detects_a_tampered_dump(store, tmp_path):
    _seed(store)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    with (snap.path / "library.dump").open("ab") as fh:
        fh.write(b"x")
    report = backup.verify(snap.path)
    assert not report.ok
    assert any("library.dump" in p for p in report.problems)


def test_verify_detects_a_table_missing_from_the_dump(store, tmp_path):
    _seed(store)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    mf = snap.path / "manifest.json"
    m = json.loads(mf.read_text())
    m["tables"]["no_such_table"] = 1
    mf.write_text(json.dumps(m))
    report = backup.verify(snap.path)
    assert any("no_such_table" in p for p in report.problems)


def test_restore_reports_a_row_count_that_differs_from_the_manifest(store, tmp_path, scratch_dsn):
    _seed(store)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    mf = snap.path / "manifest.json"
    m = json.loads(mf.read_text())
    m["tables"]["items"] = 99
    mf.write_text(json.dumps(m))
    report = backup.restore(snap.path, tmp_path / "restored", dsn=scratch_dsn)
    assert not report.ok
    assert any("items" in p and "99" in p for p in report.problems)


def test_snapshot_taken_while_writing(store, tmp_path):
    _seed(store)
    with store.connect() as conn:  # an open write transaction does not block the dump
        conn.execute("UPDATE items SET title = 'B2' WHERE id = 2")
        snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    assert backup.verify(snap.path).ok
    assert snap.manifest["item_count"] == 3


def test_missing_source_is_error(tmp_path):
    with pytest.raises(backup.BackupError):
        backup.run_backup(tmp_path / "nope", tmp_path / "bk")


def test_missing_pg_tool_names_the_setting(store, tmp_path, monkeypatch):
    _seed(store)
    monkeypatch.setenv(backup.PG_BIN_DIR_ENV, str(tmp_path / "empty"))
    with pytest.raises(backup.PgToolMissing, match="CULTURE_LIBRARY_PG_BIN_DIR"):
        backup.run_backup(store.data_dir, tmp_path / "bk")
    monkeypatch.delenv(backup.PG_BIN_DIR_ENV)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(backup.PgToolMissing, match="CULTURE_LIBRARY_PG_BIN_DIR"):
        backup.run_backup(store.data_dir, tmp_path / "bk")
    assert backup.list_snapshots(tmp_path / "bk") == []  # a failed run leaves no snapshot


def test_last_success_is_the_newest_complete_snapshot(store, tmp_path):
    assert backup.last_success(tmp_path / "bk") is None
    _seed(store)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    last = backup.last_success(tmp_path / "bk")
    assert last["snapshot"] == str(snap.path)
    assert last["created_at"] == snap.manifest["created_at"]
    assert last["items"] == 3


def test_schedule_text():
    out = backup.schedule_text("/srv/lib", "/mnt/bk", kind="systemd")
    assert "[Timer]" in out
    assert "OnCalendar=daily" in out
    assert "backup run" in out
    assert "culture-library backup run" in backup.schedule_text("/d", "/b", kind="cron")


def test_install_units_writes_user_units_and_enables_nothing(tmp_path):
    res = backup.install_units("/srv/lib", "/mnt/bk", unit_dir=tmp_path / "units")
    service = (tmp_path / "units" / "culture-library-backup.service").read_text()
    timer = (tmp_path / "units" / "culture-library-backup.timer").read_text()
    assert "ExecStart=culture-library backup run --data-dir /srv/lib --to /mnt/bk" in service
    assert "Type=oneshot" in service
    assert "OnCalendar=daily" in timer
    assert "Persistent=true" in timer
    assert "Unit=culture-library-backup.service" in timer
    assert "WantedBy=timers.target" in timer
    assert len(res["written"]) == 2
    assert "systemctl --user enable --now culture-library-backup.timer" in res["next"]
    assert all(c.startswith("systemctl --user") for c in res["next"])


# ------------------------------------------------------------------ repo-document blobs

_C1, _C2 = "a" * 40, "b" * 40


class _Pushed:
    def __init__(self, doc_id: int, sha256: str):
        self.doc_id, self.sha256 = doc_id, sha256


def _push_doc(store: Store, path: str, commit: str, content: str, visibility="private"):
    """Insert a repo document and its version with plain SQL (backup reads generic tables)."""
    sha = store.write_blob(content.encode())
    with store.connect() as conn:
        doc_id = conn.execute(
            "INSERT INTO repodocs (repo, path, kind, visibility, created_at, updated_at) "
            "VALUES ('x', %s, 'spec', %s, 't', 't') RETURNING id",
            (path, visibility),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO repodoc_versions (doc_id, commit_sha, committed_at, visibility, "
            "sha256, size, pushed_at) VALUES (%s, %s, 't', %s, %s, %s, 't')",
            (doc_id, commit, visibility, sha, len(content)),
        )
    return _Pushed(doc_id, sha)


def _push_docs(store: Store):
    a = _push_doc(store, "docs/specs/a.md", _C1, "# spec a", "public")
    b = _push_doc(store, "docs/plans/b.md", _C2, "# plan b")
    return a, b


def _blob_at(root, sha):
    return root / "files" / sha[:2] / sha


def test_repodoc_blobs_are_backed_up_restored_and_counted(store, tmp_path, scratch_dsn):
    _seed(store)
    a, b = _push_docs(store)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    m = json.loads((snap.path / "manifest.json").read_text())
    assert m["repodoc_count"] == 2
    assert m["repodoc_version_count"] == 2
    assert {v["sha256"] for v in m["repodoc_versions"]} == {a.sha256, b.sha256}
    assert m["tables"]["repodoc_versions"] == 2
    assert snap.blobs_total == 5
    assert snap.to_dict()["repodoc_versions"] == 2
    for sha in (a.sha256, b.sha256):
        assert _blob_at(tmp_path / "bk", sha).is_file()
    assert backup.verify(snap.path).ok
    target = tmp_path / "restored"
    assert backup.restore(snap.path, target, dsn=scratch_dsn).ok
    assert _blob_at(target, a.sha256).read_bytes() == b"# spec a"
    assert _blob_at(target, b.sha256).read_bytes() == b"# plan b"


def test_verify_detects_missing_repodoc_blob(store, tmp_path, scratch_dsn):
    _seed(store)
    a, _b = _push_docs(store)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    _blob_at(tmp_path / "bk", a.sha256).unlink()
    report = backup.verify(snap.path)
    assert not report.ok
    assert any(a.sha256 in p and "missing" in p for p in report.problems)
    with pytest.raises(backup.BackupError, match="verification"):
        backup.restore(snap.path, tmp_path / "restored", dsn=scratch_dsn)


def test_backup_refuses_when_a_repodoc_blob_is_missing_at_the_source(store, tmp_path):
    a, _b = _push_docs(store)
    store.blob_path(a.sha256).unlink()
    with pytest.raises(backup.BackupError, match="missing"):
        backup.run_backup(store.data_dir, tmp_path / "bk")


def _replace(store: Store, item_id: int, data: bytes) -> None:
    """What a replace does: new bytes, new sha, the old blob unlinked."""
    old = store.get_item(item_id).sha256
    new = store.write_blob(data)
    with store.connect() as conn:
        conn.execute(
            "UPDATE items SET sha256 = %s, size = %s WHERE id = %s", (new, len(data), item_id)
        )
    store.blob_path(old).unlink()


def test_replace_between_snapshot_and_blob_copy_retries_with_a_fresh_snapshot(
    store, tmp_path, monkeypatch
):
    """Lapse l1: a replace that commits after the DB snapshot unlinks a blob the snapshot
    still names. The backup takes a fresh snapshot instead of failing (or skipping it)."""
    item = store.add_item(b"# v1\n\nfirst\n", kind="document", title="a", format="md").item
    real = backup._pg_dump
    fired = []

    def snapshot_then_replace(conninfo, snapshot_id, dst):
        real(conninfo, snapshot_id, dst)
        if not fired:  # the race: the file changes right after the snapshot
            fired.append(True)
            _replace(store, item.id, b"# v2\n\nsecond\n")

    monkeypatch.setattr(backup, "_pg_dump", snapshot_then_replace)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    assert snap.manifest["items"][0]["sha256"] == store.get_item(item.id).sha256
    assert backup.verify(snap.path).ok
    assert len(backup.list_snapshots(tmp_path / "bk")) == 1


def test_blob_missing_while_still_referenced_still_fails(store, tmp_path):
    item = store.add_item(b"# v1\n\nfirst\n", kind="document", title="a", format="md").item
    store.blob_path(item.sha256).unlink()
    with pytest.raises(backup.BackupError, match="missing"):
        backup.run_backup(store.data_dir, tmp_path / "bk")


def test_blob_unlinked_during_copy_also_retries(store, tmp_path, monkeypatch):
    """The race can also land between the is_file() check and the copy itself."""
    item = store.add_item(b"# v1\n\nfirst\n", kind="document", title="a", format="md").item
    real_copy = backup.shutil.copyfile
    fired = []

    def racing_copy(src, dst, *a, **kw):
        if not fired:
            fired.append(True)
            _replace(store, item.id, b"# v2\n\nsecond\n")
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(backup.shutil, "copyfile", racing_copy)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    assert backup.verify(snap.path).ok
