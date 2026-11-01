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

A = "a" * 40
REMOTE = "https://github.com/agentculture/lobes-cli"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _push(store: Store, path: str, content: bytes, commit: str = A):
    from culture_library.core.repodocs import RepoDocs  # imported late: ported separately

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


def test_reconcile_covers_drafts(store: Store):
    r = _push(store, "docs/a.md", b"# a")
    doc_id = r.doc.id
    sha = store.write_blob(b"draft body")
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO repodoc_drafts (doc_id, sha256, size, pushed_at) VALUES (%s, %s, %s, %s)",
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


def test_two_concurrent_enqueues_of_one_key_leave_one_row(store: Store):
    """Acceptance 1: enqueue is one INSERT ... ON CONFLICT (key), so a race cannot duplicate."""
    import threading

    sha = store.write_blob(b"race")
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker():
        try:
            with store.connect() as conn:
                barrier.wait(timeout=10)
                enqueue(conn, "same-key", sha, "text/plain")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    rows = _rows(store)
    assert [(r["key"], r["state"]) for r in rows] == [("same-key", "pending")]


def test_outbox_check_needs_no_widening_migration():
    from culture_library.core import mirror

    assert not hasattr(mirror, "widen_outbox_state")
    assert not hasattr(mirror, "register_schema")


def test_has_table_uses_the_catalog(store: Store):
    from culture_library.core.mirror import _has_table

    with store.reader() as conn:
        assert _has_table(conn, "mirror_outbox")
        assert not _has_table(conn, "no_such_table")


def test_reconcile_reads_versions_and_drafts_without_repodocs_module(store: Store):
    sha_v, sha_d = store.write_blob(b"# v"), store.write_blob(b"draft")
    with store.connect() as conn:
        doc_id = conn.execute(
            "INSERT INTO repodocs (repo, path, kind, created_at, updated_at)"
            " VALUES ('r', 'docs/a.md', 'spec', 't', 't') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO repodoc_versions (doc_id, commit_sha, committed_at, sha256, size,"
            " pushed_at) VALUES (%s, %s, 't', %s, 3, 't')",
            (doc_id, A, sha_v),
        )
        conn.execute(
            "INSERT INTO repodoc_drafts (doc_id, sha256, size, pushed_at) VALUES (%s, %s, 5, 't')",
            (doc_id, sha_d),
        )
    backend = FakeBackend()
    assert reconcile(store, backend) == 2
    assert drain(store, backend)["done"] == 2
    assert backend.objects[f"r/docs/a.md@{A}"][0] == b"# v"
    assert backend.objects["r/docs/a.md@working"][0] == b"draft"
    assert reconcile(store, backend) == 0
