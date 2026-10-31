"""Spec links: PR, merge commit, current code paths, newer-wins (plan task t3)."""

from __future__ import annotations

import pytest

from culture_library.core import Store, ValidationError
from culture_library.core.speclinks import SpecLinks

T1 = "2026-10-01T10:00:00Z"
T2 = "2026-10-02T10:00:00+00:00"
T3 = "2026-10-03T10:00:00Z"


@pytest.fixture
def sl(store: Store) -> SpecLinks:
    return SpecLinks(store)


def test_no_links_is_empty_structure(sl):
    got = sl.links(7)
    assert got["pr"] is None
    assert got["merge_commit"] is None
    assert got["code_paths"] == []
    assert got["trailer_commits"] == []


def test_set_and_read(sl):
    sl.set_links(
        1,
        pr={"url": "https://github.com/a/b/pull/9", "number": 9},
        merge_commit="m" * 40,
        code_paths=["b.py", "a.py", "a.py"],
        at_commit="c1",
        at_committed_at=T1,
        trailer_commits=["t2", "t1"],
    )
    got = sl.links(1)
    assert got["pr"] == {"url": "https://github.com/a/b/pull/9", "number": 9}
    assert got["merge_commit"] == "m" * 40
    assert got["code_paths"] == ["a.py", "b.py"]
    assert got["at_commit"] == "c1"
    assert got["trailer_commits"] == ["t1", "t2"]
    assert sl.links(2)["code_paths"] == []  # keyed per doc


def test_newer_replaces_older_never_regresses(sl):
    sl.set_links(1, code_paths=["old.py"], at_commit="c1", at_committed_at=T2)
    sl.set_links(1, code_paths=["renamed.py"], at_commit="c3", at_committed_at=T3)
    assert sl.links(1)["code_paths"] == ["renamed.py"]
    got = sl.set_links(1, code_paths=["old.py"], at_commit="c2", at_committed_at=T1)
    assert got["code_paths"] == ["renamed.py"]
    assert got["at_commit"] == "c3"
    # equal time, different commit: the latest push wins (review finding 4)
    sl.set_links(1, code_paths=["x.py"], at_commit="cx", at_committed_at=T3)
    assert sl.links(1)["code_paths"] == ["x.py"]
    assert sl.links(1)["at_commit"] == "cx"


def test_record_reports_whether_the_links_were_applied(sl):
    """Review finding 4: the pusher must learn when its code paths were not stored."""
    links, applied = sl.record(1, code_paths=["a.py"], at_commit="c2", at_committed_at=T2)
    assert applied is True
    assert links["code_paths"] == ["a.py"]
    links, applied = sl.record(1, code_paths=["old.py"], at_commit="c1", at_committed_at=T1)
    assert applied is False
    assert links["code_paths"] == ["a.py"]
    assert sl.record(1, code_paths=["b.py"], at_commit="c2b", at_committed_at=T2)[1] is True
    assert sl.record(1, code_paths=["b.py"], at_commit="c2b", at_committed_at=T2)[1] is True
    # no code paths: pr and merge_commit always apply
    assert sl.record(1, pr=3, merge_commit="m")[1] is True


def test_pr_and_merge_without_paths_keep_paths(sl):
    sl.set_links(1, code_paths=["a.py"], at_commit="c1", at_committed_at=T1)
    sl.set_links(1, pr=12, merge_commit="abc")
    got = sl.links(1)
    assert got["pr"] == {"url": None, "number": 12}
    assert got["merge_commit"] == "abc"
    assert got["code_paths"] == ["a.py"]


def test_validation(sl):
    with pytest.raises(ValidationError):
        sl.set_links(1, code_paths=["a.py"])
    with pytest.raises(ValidationError):
        sl.set_links(1, code_paths=["a.py"], at_commit="c", at_committed_at="yesterday")
    with pytest.raises(ValidationError):
        sl.set_links(1, code_paths="a.py", at_commit="c", at_committed_at=T1)


# --- concurrency (plan library-on-postgres-and-pgvector, task t11) --------------------


def _race(store: Store, monkeypatch, first, second) -> list:
    """``second`` runs while ``first`` sleeps inside its transaction, after one statement."""
    import threading
    import time
    from contextlib import contextmanager

    real = store.connect
    started = threading.Event()
    owner: list[threading.Thread] = []

    class Pausing:
        def __init__(self, conn) -> None:
            self._conn, self._fired = conn, False

        def execute(self, *args, **kwargs):
            cur = self._conn.execute(*args, **kwargs)
            if not self._fired:
                self._fired = True
                started.set()
                time.sleep(0.5)
            return cur

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextmanager
    def connect():
        with real() as conn:
            yield Pausing(conn) if threading.current_thread() is owner[0] else conn

    monkeypatch.setattr(store, "connect", connect)
    results: list = [None, None]

    def run(i, fn):
        try:
            results[i] = fn()
        except BaseException as exc:  # re-raised on the test thread
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
    monkeypatch.undo()
    return results


def _rows(store: Store) -> list:
    with store.reader() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT doc_id, pr_url, pr_number, merge_commit, code_paths_json, at_commit,"
                " at_committed_at, trailer_commits_json FROM spec_links ORDER BY doc_id"
            )
        ]


def test_two_concurrent_records_end_as_a_serial_run(store, monkeypatch):
    """Criterion 2 (t11): two pushes for one spec at once leave exactly the serial rows."""
    sl = SpecLinks(store)
    newer = dict(pr=5, code_paths=["new.py"], at_commit="c2", at_committed_at=T2)
    older = dict(merge_commit="m", code_paths=["old.py"], at_commit="c1", at_committed_at=T1)
    (first, applied_first), (second, applied_second) = _race(
        store, monkeypatch, lambda: sl.record(1, **newer), lambda: sl.record(1, **older)
    )
    assert (applied_first, applied_second) == (True, False)
    assert second["code_paths"] == ["new.py"]
    sl.record(2, **newer)
    sl.record(2, **older)
    raced, serial = _rows(store)
    assert raced[1:] == serial[1:]
    assert raced[4] == ["new.py"]  # jsonb comes back as a list


def test_two_concurrent_first_records_both_apply(store, monkeypatch):
    sl = SpecLinks(store)
    one, two = _race(
        store,
        monkeypatch,
        lambda: sl.record(1, pr=7),
        lambda: sl.record(1, code_paths=["a.py"], at_commit="c", at_committed_at=T1),
    )
    assert one[1] and two[1]
    got = sl.links(1)
    assert got["pr"] == {"url": None, "number": 7}
    assert got["code_paths"] == ["a.py"]
    assert len(_rows(store)) == 1
