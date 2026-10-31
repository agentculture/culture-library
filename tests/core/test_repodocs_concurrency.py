"""Concurrent pushes of one repo doc (plan library-on-postgres-and-pgvector, task t11).

Postgres runs writers in parallel under READ COMMITTED, so a look-then-insert that SQLite's
``BEGIN IMMEDIATE`` serialised can race. The first push is paused right after its first
doc lookup (its ``FOR UPDATE``) while the second runs, then both must end with the rows a
serial run produces.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

import pytest

from culture_library.core import Store
from culture_library.core.repodocs import WORKING, RepoDocs

A = "a" * 40
B = "b" * 40
REMOTE = "https://github.com/agentculture/devague"
PAUSE_S = 0.5


class _PausingConn:
    """A connection that runs ``after_first()`` once, after its first ``FOR UPDATE``."""

    def __init__(self, conn, after_first) -> None:
        self._conn = conn
        self._after_first = after_first
        self._fired = False

    def execute(self, *args, **kwargs):
        cur = self._conn.execute(*args, **kwargs)
        if not self._fired and "FOR UPDATE" in str(args[0]):
            self._fired = True
            self._after_first()
        return cur

    def __getattr__(self, name):
        return getattr(self._conn, name)


def race(store: Store, monkeypatch, first, second) -> list:
    """Run ``first`` and ``second`` in threads; ``second`` starts once ``first`` has looked up
    (and, if it exists, locked) the doc, and runs while ``first`` sleeps in its transaction."""
    real = store.connect
    started = threading.Event()
    owner: list[threading.Thread] = []

    def pause() -> None:
        started.set()
        time.sleep(PAUSE_S)

    @contextmanager
    def connect():
        with real() as conn:
            if owner and threading.current_thread() is owner[0]:
                yield _PausingConn(conn, pause)
            else:
                yield conn

    monkeypatch.setattr(store, "connect", connect)
    results: list = [None, None]

    def run(i: int, fn) -> None:
        try:
            results[i] = fn()
        except BaseException as exc:  # reported to the test thread below
            results[i] = exc

    t1 = threading.Thread(target=run, args=(0, first))
    owner.append(t1)
    t2 = threading.Thread(target=lambda: (started.wait(10), run(1, second)))
    t1.start()
    t2.start()
    t1.join(30)
    t2.join(30)
    for got in results:
        if isinstance(got, BaseException):
            raise got
    return results


def _commit_push(docs: RepoDocs, commit: str, when: str, path: str = "docs/specs/x.md"):
    return docs.push(
        remote=REMOTE,
        path=path,
        commit=commit,
        committed_at=when,
        default_branch_head=commit,
        content=f"{commit[:1]}\n",
    )


def _state(store: Store, path: str) -> dict:
    with store.reader() as conn:
        doc = conn.execute(
            "SELECT d.kind, d.visibility, v.commit_sha AS current"
            " FROM repodocs d LEFT JOIN repodoc_versions v ON v.id = d.current_version_id"
            " WHERE d.path = %s",
            (path,),
        ).fetchall()
        versions = conn.execute(
            "SELECT v.commit_sha, v.committed_at, v.on_default_branch, v.sha256"
            " FROM repodoc_versions v JOIN repodocs d ON d.id = v.doc_id WHERE d.path = %s"
            " ORDER BY v.commit_sha",
            (path,),
        ).fetchall()
        drafts = conn.execute(
            "SELECT r.sha256 FROM repodoc_drafts r JOIN repodocs d ON d.id = r.doc_id"
            " WHERE d.path = %s",
            (path,),
        ).fetchall()
    return {
        "docs": [tuple(r) for r in doc],
        "versions": [tuple(r) for r in versions],
        "drafts": [tuple(r) for r in drafts],
    }


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "d")


def test_two_concurrent_first_pushes_make_one_doc(store, monkeypatch) -> None:
    docs = RepoDocs(store)
    one, two = race(
        store,
        monkeypatch,
        lambda: _commit_push(docs, A, "2026-10-01T00:00:00Z"),
        lambda: _commit_push(docs, B, "2026-10-02T00:00:00Z"),
    )
    assert one.doc.id == two.doc.id
    assert one.created and two.created
    monkeypatch.undo()
    _commit_push(docs, A, "2026-10-01T00:00:00Z", path="docs/specs/serial.md")
    _commit_push(docs, B, "2026-10-02T00:00:00Z", path="docs/specs/serial.md")
    assert _state(store, "docs/specs/x.md") == _state(store, "docs/specs/serial.md")
    assert _state(store, "docs/specs/x.md")["docs"] == [("spec", "private", B)]


def test_two_concurrent_pushes_of_one_commit_make_one_version(store, monkeypatch) -> None:
    docs = RepoDocs(store)
    _commit_push(docs, A, "2026-10-01T00:00:00Z")
    one, two = race(
        store,
        monkeypatch,
        lambda: _commit_push(docs, B, "2026-10-02T00:00:00Z"),
        lambda: _commit_push(docs, B, "2026-10-02T00:00:00Z"),
    )
    assert sorted([one.created, two.created]) == [False, True]
    assert len(_state(store, "docs/specs/x.md")["versions"]) == 2


def test_two_concurrent_working_pushes_keep_one_draft(store, monkeypatch) -> None:
    docs = RepoDocs(store)

    def working(text: str):
        return lambda: docs.push(
            remote=REMOTE, path="docs/specs/x.md", commit=WORKING, content=text
        )

    working("zero\n")()  # the doc exists, so the first push holds its lock while it sleeps
    one, two = race(store, monkeypatch, working("one\n"), working("two\n"))
    assert one.doc.id == two.doc.id
    state = _state(store, "docs/specs/x.md")
    assert len(state["docs"]) == 1
    assert state["drafts"] == [(two.draft.sha256,)]  # the second waited, so it wrote last


def test_tombstone_waits_for_a_concurrent_push(store, monkeypatch) -> None:
    docs = RepoDocs(store)
    _commit_push(docs, A, "2026-10-01T00:00:00Z")
    pushed, removed = race(
        store,
        monkeypatch,
        lambda: _commit_push(docs, B, "2026-10-02T00:00:00Z"),
        lambda: docs.tombstone(REMOTE, "docs/specs/x.md", B),
    )
    assert pushed.created
    assert removed.removed_commit == B
    assert removed.current_commit == B  # it saw the push's version, so it ran after it
