"""Backup covers working drafts and the pending mirror outbox (plan task t12)."""

from culture_library.core import Store, backup, mirror
from culture_library.core.repodocs import RepoDocs

REMOTE = "https://github.com/agentculture/x.git"


def _blob_in(root, sha):
    return (root / "files" / sha[:2] / sha).is_file()


def _seed(store: Store):
    docs = RepoDocs(store)
    draft = docs.push(remote=REMOTE, path="docs/d.md", commit="working", content="# draft")
    queued = docs.push(remote=REMOTE, path="docs/q.md", commit="working", content="# queued")
    gone = docs.push(remote=REMOTE, path="docs/g.md", commit="working", content="# outbox only")
    with store.connect() as conn:
        mirror.enqueue(conn, "x/docs/q.md@working", queued.draft.sha256, "text/markdown")
        mirror.enqueue(conn, "x/docs/g.md@working", gone.draft.sha256, "text/markdown")
    docs.tombstone(REMOTE, "docs/g.md", "working")  # drops the draft, leaves the outbox row
    return docs, draft, queued, gone


def test_backup_verify_finds_draft_and_outbox_blobs(store, tmp_path):
    _docs, draft, queued, gone = _seed(store)
    only_outbox = gone.draft.sha256
    with store.reader() as conn:
        n = conn.execute(
            "SELECT count(*) FROM repodoc_drafts WHERE sha256 = ?", (only_outbox,)
        ).fetchone()[0]
        assert n == 0
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    for sha in (draft.draft.sha256, queued.draft.sha256, only_outbox):
        assert _blob_in(tmp_path / "bk", sha)
    assert backup.verify(snap.path).ok
    # remove the outbox-only blob from the backup: verify must notice
    (tmp_path / "bk" / "files" / only_outbox[:2] / only_outbox).unlink()
    assert not backup.verify(snap.path).ok


def test_restore_brings_back_drafts_and_pending_outbox(store, tmp_path):
    docs, draft, queued, gone = _seed(store)
    snap = backup.run_backup(store.data_dir, tmp_path / "bk")
    target = tmp_path / "restored"
    assert backup.restore(snap.path, target).ok  # no S3 backend configured anywhere
    restored = Store(target)
    rdocs = RepoDocs(restored)
    assert rdocs.content(draft.doc.id, "working", include_private=True) == b"# draft"
    assert rdocs.content(queued.doc.id, "working", include_private=True) == b"# queued"
    with restored.reader() as conn:
        rows = conn.execute("SELECT key, sha256, state FROM mirror_outbox ORDER BY key").fetchall()
        assert conn.execute("SELECT count(*) FROM repodoc_drafts").fetchone()[0] == 2
    assert [tuple(r) for r in rows] == [
        ("x/docs/g.md@working", gone.draft.sha256, "pending"),
        ("x/docs/q.md@working", queued.draft.sha256, "pending"),
    ]
    for r in rows:
        assert restored.blob_path(r[1]).is_file()
    assert backup.verify(snap.path).ok
