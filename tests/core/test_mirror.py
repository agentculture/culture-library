"""Mirror core: keys, outbox, drain, reconcile, stats (plan task t4)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from culture_library.core import Store
from culture_library.core.mirror import (
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
    assert key_for("r", "p.md", A) == key_for("r", "p.md", A)


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
    assert drain(store, backend) == {"done": 1, "failed": 0}
    assert backend.objects["k1"] == (b"hello", "text/plain")
    assert _rows(store)[0]["state"] == "done"
    assert drain(store, backend) == {"done": 0, "failed": 0}


def test_enqueue_is_on_callers_connection_and_rolls_back(store: Store):
    sha = store.write_blob(b"x")
    with pytest.raises(RuntimeError):
        with store.connect() as conn:
            enqueue(conn, "k", sha)
            raise RuntimeError
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
    assert drain(store, backend) == {"done": 0, "failed": 1}
    assert drain(store, backend)["failed"] == 1
    row = _rows(store)[0]
    assert row["state"] == "pending" and row["attempts"] == 2 and "boom" in row["last_error"]
    backend.fail = None
    assert drain(store, backend)["done"] == 1
    row = _rows(store)[0]
    assert row["state"] == "done" and row["last_error"] == ""


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
    assert pending_stats(store) == {"count": 0, "oldest_age_seconds": None}
    sha = store.write_blob(b"x")
    with store.connect() as conn:
        enqueue(conn, "k1", sha)
        enqueue(conn, "k2", sha)
    later = datetime.now(UTC) + timedelta(seconds=100)
    stats = pending_stats(store, at=later)
    assert stats["count"] == 2 and 99 <= stats["oldest_age_seconds"] <= 102
    drain(store, FakeBackend())
    assert pending_stats(store)["count"] == 0
