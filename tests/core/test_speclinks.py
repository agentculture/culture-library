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
