"""Mirror core: keys, outbox, drain, reconcile, stats (plan task t4)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from culture_library.core import Store
from culture_library.core.mirror import (
    MAX_ATTEMPTS,
    FakeBackend,
    MirrorBackend,
    drain,
    enqueue,
    key_for,
    pending_stats,
    reconcile,
)
from culture_library.core.repodocs import RepoDocs

A = "a" * 40
REMOTE = "https://github.com/agentculture/lobes-cli"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _push(store: Store, path: str, content: bytes, commit: str = A):
    return RepoDocs(store).push(
        remote=REMOTE,
        path=path,
        commit=commit,
        committed_at="2026-10-01T00:00:00Z",
        default_branch_head=commit,
        content=content,
    )


def _rows(store: Store):
    with store.reader() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM mirror_outbox ORDER BY id")]


def test_key_for():
    assert key_for("github.com/agentculture/x", "docs/a.md", A) == (
        f"github.com/agentculture/x/docs/a.md@{A}"
    )
    assert key_for("r", "p.md", None).endswith("@working")
    assert key_for("r", "p.md", "working") == "r/p.md@working"
    first = key_for("r", "p.md", A)
    assert key_for("r", "p.md", A) == first


def test_fake_backend_satisfies_protocol():
    b: MirrorBackend = FakeBackend()
    b.put("k", b"x", "text/plain")
    assert b.head("k") == _sha(b"x")
    assert b.head("nope") is None


def test_enqueue_and_drain_success(store: Store):
    sha = store.write_blob(b"hello")
    with store.connect() as conn:
        enqueue(conn, "k1", sha, "text/plain")
    row = _rows(store)[0]
    assert (row["state"], row["attempts"], row["last_error"], row["sha256"]) == (
        "pending",
        0,
        "",
        sha,
    )
    backend = FakeBackend()
    assert drain(store, backend) == {"done": 1, "failed": 0, "parked": 0, "retry": 0}
    assert backend.objects["k1"] == (b"hello", "text/plain")
    assert _rows(store)[0]["state"] == "done"
    assert drain(store, backend) == {"done": 0, "failed": 0, "parked": 0, "retry": 0}


def test_enqueue_is_on_callers_connection_and_rolls_back(store: Store):
    sha = store.write_blob(b"x")

    def enqueue_then_fail():
        with store.connect() as conn:
            enqueue(conn, "k", sha)
            raise RuntimeError

    with pytest.raises(RuntimeError):
        enqueue_then_fail()
    assert _rows(store) == []


def test_enqueue_same_sha_noop_changed_sha_requeues(store: Store):
    s1, s2 = store.write_blob(b"1"), store.write_blob(b"2")
    with store.connect() as conn:
        enqueue(conn, "k", s1)
    drain(store, FakeBackend())
    with store.connect() as conn:
        enqueue(conn, "k", s1)
    assert _rows(store)[0]["state"] == "done"
    with store.connect() as conn:
        enqueue(conn, "k", s2)
    row = _rows(store)[0]
    assert (row["state"], row["sha256"], row["attempts"]) == ("pending", s2, 0)


def test_drain_failure_records_and_does_not_raise(store: Store):
    sha = store.write_blob(b"x")
    with store.connect() as conn:
        enqueue(conn, "k", sha)
    backend = FakeBackend()
    backend.fail = OSError("boom")
    assert drain(store, backend) == {"done": 0, "failed": 1, "parked": 0, "retry": 1}
    assert drain(store, backend)["failed"] == 1
    row = _rows(store)[0]
    assert row["state"] == "pending"
    assert row["attempts"] == 2
    assert "boom" in row["last_error"]
    backend.fail = None
    assert drain(store, backend)["done"] == 1
    row = _rows(store)[0]
    assert row["state"] == "done"
    assert row["last_error"] == ""


def test_drain_missing_blob_is_recorded(store: Store):
    with store.connect() as conn:
        enqueue(conn, "k", "0" * 64)
    assert drain(store, FakeBackend())["failed"] == 1
    assert _rows(store)[0]["attempts"] == 1


def test_reconcile_versions(store: Store):
    _push(store, "docs/a.md", b"# a")
    backend = FakeBackend()
    assert reconcile(store, backend) == 1
    drain(store, backend)
    assert reconcile(store, backend) == 0
    key = key_for("github.com/agentculture/lobes-cli", "docs/a.md", A)
    assert key in backend.objects
    backend.objects[key] = (b"tampered", "x")  # sha differs
    assert reconcile(store, backend) == 1
    drain(store, backend)
    assert backend.objects[key][0] == b"# a"
    del backend.objects[key]  # missing
    assert reconcile(store, backend) == 1


def test_reconcile_without_drafts_table(store: Store):
    # the repodoc_drafts table does not exist on this branch; reconcile must skip it cleanly
    _push(store, "docs/a.md", b"# a")
    assert reconcile(store, FakeBackend()) == 1


def test_reconcile_covers_drafts(store: Store):
    r = _push(store, "docs/a.md", b"# a")
    doc_id = r.doc.id
    sha = store.write_blob(b"draft body")
    with store.connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS repodoc_drafts (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " doc_id INTEGER NOT NULL UNIQUE, sha256 TEXT NOT NULL, size INTEGER NOT NULL,"
            " pushed_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO repodoc_drafts (doc_id, sha256, size, pushed_at) VALUES (?,?,?,?)",
            (doc_id, sha, 10, "2026-10-02T00:00:00+00:00"),
        )
    backend = FakeBackend()
    assert reconcile(store, backend) == 2
    drain(store, backend)
    draft_key = "github.com/agentculture/lobes-cli/docs/a.md@working"
    assert backend.objects[draft_key][0] == b"draft body"
    assert reconcile(store, backend) == 0


def test_pending_stats(store: Store):
    assert pending_stats(store) == {"count": 0, "oldest_age_seconds": None, "parked": 0}
    sha = store.write_blob(b"x")
    with store.connect() as conn:
        enqueue(conn, "k1", sha)
        enqueue(conn, "k2", sha)
    later = datetime.now(UTC) + timedelta(seconds=100)
    stats = pending_stats(store, at=later)
    assert stats["count"] == 2
    assert 99 <= stats["oldest_age_seconds"] <= 102
    drain(store, FakeBackend())
    assert pending_stats(store)["count"] == 0


def _park(store: Store, key: str = "k", sha: str = "0" * 64) -> None:
    with store.connect() as conn:
        enqueue(conn, key, sha)
    for _ in range(MAX_ATTEMPTS - 1):
        assert drain(store, FakeBackend())["retry"] == 1
    assert drain(store, FakeBackend()) == {"done": 0, "failed": 1, "parked": 1, "retry": 0}


def test_a_row_that_always_fails_is_parked(store: Store):
    """Review finding 8: a never-uploadable row leaves the pending set after MAX_ATTEMPTS."""
    assert MAX_ATTEMPTS == 8
    _park(store)  # the blob is missing: FileNotFoundError every pass
    row = _rows(store)[0]
    assert (row["state"], row["attempts"]) == ("failed", MAX_ATTEMPTS)
    assert "FileNotFoundError" in row["last_error"]
    assert pending_stats(store)["count"] == 0
    assert pending_stats(store)["parked"] == 1
    assert drain(store, FakeBackend()) == {"done": 0, "failed": 0, "parked": 0, "retry": 0}


def test_a_parked_row_does_not_delay_a_healthy_one(store: Store):
    _park(store, "bad")
    sha = store.write_blob(b"ok")
    with store.connect() as conn:
        enqueue(conn, "good", sha)
    backend = FakeBackend()
    assert drain(store, backend) == {"done": 1, "failed": 0, "parked": 0, "retry": 0}
    assert "good" in backend.objects


def test_reconcile_requeues_a_parked_key(store: Store):
    r = _push(store, "docs/a.md", b"# a")
    key = key_for(r.doc.repo, "docs/a.md", A)
    blob = store.blob_path(r.version.sha256)
    saved = blob.read_bytes()
    blob.unlink()
    _park(store, key, r.version.sha256)
    blob.write_bytes(saved)
    backend = FakeBackend()
    assert reconcile(store, backend) == 1
    row = _rows(store)[0]
    assert (row["state"], row["attempts"], row["last_error"]) == ("pending", 0, "")
    assert drain(store, backend)["done"] == 1
    assert backend.objects[key][0] == b"# a"


def test_enqueue_same_sha_requeues_a_parked_row(store: Store):
    _park(store)
    with store.connect() as conn:
        enqueue(conn, "k", "0" * 64)
    row = _rows(store)[0]
    assert (row["state"], row["attempts"]) == ("pending", 0)


def test_old_outbox_check_is_widened_on_open(tmp_path):
    """A database from before parking (CHECK pending/done only) accepts 'failed'."""
    import sqlite3

    data = tmp_path / "data"
    data.mkdir()
    conn = sqlite3.connect(data / "library.db")
    conn.executescript(
        "CREATE TABLE mirror_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " key TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL,"
        " content_type TEXT NOT NULL DEFAULT 'application/octet-stream',"
        " state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done')),"
        " attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',"
        " enqueued_at TEXT NOT NULL, done_at TEXT);"
        "INSERT INTO mirror_outbox (key, sha256, enqueued_at) VALUES ('k', 'x', 't');"
    )
    conn.commit()
    conn.close()
    store = Store(data)
    with store.connect() as c:
        c.execute("UPDATE mirror_outbox SET state = 'failed' WHERE key = 'k'")
    assert [(r["key"], r["state"], r["id"]) for r in _rows(store)] == [("k", "failed", 1)]
    Store(data)  # idempotent
    assert len(_rows(store)) == 1
