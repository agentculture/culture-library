from __future__ import annotations

import threading
import time

import pytest

from culture_library.core import ValidationError
from culture_library.core.feed import EVENT_TYPES, append, read_since

SHA = "a" * 64


class _Recorder:
    """Wraps a connection and records the SQL text of every ``execute``."""

    def __init__(self, conn, log):
        self._conn, self._log = conn, log

    def execute(self, query, *args, **kwargs):
        self._log.append(str(query))
        return self._conn.execute(query, *args, **kwargs)


def _ev(type_, item_id=1, **kw):
    return {"type": type_, "item_id": item_id, "sha256": SHA, "kind": "book", **kw}


def test_append_all_types_monotonic(store):
    cursors = []
    with store.connect() as conn:
        for t in EVENT_TYPES:
            cursors.append(append(conn, _ev(t)))
    assert set(EVENT_TYPES) == {"created", "updated", "archived", "purged", "send_requested"}
    assert cursors == sorted(cursors)
    assert len(set(cursors)) == len(cursors)


def test_rejects_bad_type(store):
    bad = _ev("exploded")
    with store.connect() as conn:
        with pytest.raises(ValidationError):
            append(conn, bad)


def test_event_fields_and_data(store):
    with store.connect() as conn:
        append(conn, _ev("send_requested", visibility="public", data={"target": "xteink"}))
    with store.reader() as conn:
        (e,) = read_since(conn, 0)["events"]
    assert e["type"] == "send_requested"
    assert e["item_id"] == 1
    assert e["sha256"] == SHA
    assert e["kind"] == "book"
    assert e["visibility"] == "public"
    assert e["at"]
    assert e["data"] == {"target": "xteink"}


def test_append_rolls_back_with_caller_txn(store):
    def append_then_fail():
        with store.connect() as conn:
            append(conn, _ev("created"))
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        append_then_fail()
    with store.reader() as conn:
        assert read_since(conn, 0)["events"] == []


def test_reader_from_saved_cursor_sees_exactly_new_events_no_catalog_scan(store):
    with store.connect() as conn:
        append(conn, _ev("created", 1))
    with store.reader() as conn:
        saved = read_since(conn, 0)["cursor"]
    with store.connect() as conn:
        mix = [
            append(conn, _ev("created", 2)),
            append(conn, _ev("updated", 1)),
            append(conn, _ev("archived", 2)),
            append(conn, _ev("purged", 2)),
        ]
    queries: list[str] = []
    with store.reader() as conn:
        out = read_since(_Recorder(conn, queries), saved)
    assert [e["cursor"] for e in out["events"]] == mix
    assert [e["type"] for e in out["events"]] == ["created", "updated", "archived", "purged"]
    assert out["cursor"] == mix[-1]
    assert out["more"] is False
    assert queries
    assert not any("items" in q.lower() for q in queries)
    with store.reader() as conn:
        assert read_since(conn, out["cursor"])["events"] == []


def test_limit_and_more(store):
    with store.connect() as conn:
        for i in range(5):
            append(conn, _ev("created", i))
    with store.reader() as conn:
        a = read_since(conn, 0, limit=3)
        b = read_since(conn, a["cursor"], limit=3)
    assert len(a["events"]) == 3
    assert a["more"] is True
    assert len(b["events"]) == 2
    assert b["more"] is False


def test_hide_private_keeps_cursor_advancing(store):
    with store.connect() as conn:
        append(conn, _ev("created", 1, visibility="public"))
        last = append(conn, _ev("created", 2, visibility="private"))
    with store.reader() as conn:
        out = read_since(conn, 0, include_private=False)
    assert [e["item_id"] for e in out["events"]] == [1]
    assert out["cursor"] == last


# ------------------------------------------------------------------ one item (d4 item 4)


def test_read_since_filters_one_item_using_the_index(store):
    with store.connect() as conn:
        for item_id in (1, 2, 1, 3, 1):
            append(conn, _ev("updated", item_id=item_id, visibility="public"))
    with store.reader() as conn:
        page = read_since(conn, 0, item_id=1)
        assert [e["item_id"] for e in page["events"]] == [1, 1, 1]
        assert page["more"] is False
        assert page["cursor"] == page["events"][-1]["cursor"]
        first = read_since(conn, 0, item_id=1, limit=2)
        assert len(first["events"]) == 2
        assert first["more"] is True
        rest = read_since(conn, first["cursor"], item_id=1)
        assert len(rest["events"]) == 1
        assert rest["more"] is False
        assert read_since(conn, 0, item_id=99) == {"events": [], "cursor": 0, "more": False}
        # the query read_since actually runs is answered from the feed(item_id) index
        traced: list[str] = []
        read_since(_Recorder(conn, traced), 0, item_id=1)
        sql = next(t for t in traced if "FROM feed" in t)
        conn.execute("SET LOCAL enable_seqscan = off")  # a tiny table would otherwise scan
        # one placeholder per bind in read_since's item_id query: item_id, since, limit + 1
        plan = " ".join(r[0] for r in conn.execute("EXPLAIN " + sql, (1, 0, 501)))
        assert "feed_item_id" in plan


def test_read_since_one_private_item_needs_access(store):
    with store.connect() as conn:
        append(conn, _ev("created", item_id=5, visibility="private"))
    with store.reader() as conn:
        assert read_since(conn, 0, item_id=5, include_private=False)["events"] == []
        assert len(read_since(conn, 0, item_id=5)["events"]) == 1


def test_read_since_shows_private_events_to_their_uploader(store):
    from culture_library.core import uploaders

    with store.connect() as conn:
        append(conn, _ev("created", item_id=7, visibility="private"))
        append(conn, _ev("created", item_id=8, visibility="private"))
        uploaders.record(conn, 7, "kid")
    with store.reader() as conn:
        mine = read_since(conn, 0, include_private=False, owner="kid")
        assert [e["item_id"] for e in mine["events"]] == [7]
        assert read_since(conn, 0, include_private=False, owner="other")["events"] == []
        assert read_since(conn, 0, include_private=False)["cursor"] == mine["cursor"]


def test_repodoc_events_interleave_in_cursor_order(store):
    from culture_library.core.feed import append_repodoc

    with store.connect() as conn:
        c1 = append(conn, _ev("created", item_id=5))
        c2 = append_repodoc(conn, 5, "spec", SHA, {"repo": "r", "path": "a.md", "commit": "abc"})
        c3 = append_repodoc(conn, 6, "plan", SHA, {"repo": "r", "path": "b.md"})
        c4 = append(conn, _ev("updated", item_id=5))
    with store.reader() as conn:
        # review finding 5: no subject keeps the pre-repodoc behaviour, items only
        assert [e["cursor"] for e in read_since(conn, 0)["events"]] == [c1, c4]
        evs = read_since(conn, 0, subject="all")["events"]
        assert [e["cursor"] for e in evs] == [c1, c2, c3, c4]
        assert [e["subject"] for e in evs] == ["item", "repodoc", "repodoc", "item"]
        assert evs[1]["data"] == {"repo": "r", "path": "a.md", "commit": "abc"}
        assert evs[2]["data"]["commit"] == "working"
        assert [e["cursor"] for e in read_since(conn, 0, subject="item")["events"]] == [c1, c4]
        assert [e["cursor"] for e in read_since(conn, 0, item_id=5)["events"]] == [c1, c4]
        assert [e["cursor"] for e in read_since(conn, 0, subject="repodoc")["events"]] == [c2, c3]


def test_repodoc_private_hidden_from_public_and_owner(store):
    from culture_library.core.feed import append_repodoc

    with store.connect() as conn:
        append_repodoc(conn, 1, "spec", SHA, {"repo": "r", "path": "a"}, visibility="public")
        append_repodoc(conn, 2, "spec", SHA, {"repo": "r", "path": "b"})
    with store.reader() as conn:
        shown = read_since(conn, 0, include_private=False, owner="k", subject="all")["events"]
        assert [e["item_id"] for e in shown] == [1]


def test_repodoc_validation(store):
    from culture_library.core.feed import append_repodoc

    with store.connect() as conn:
        with pytest.raises(ValidationError):
            append_repodoc(conn, 1, "book", SHA, {"repo": "r", "path": "a"})
        with pytest.raises(ValidationError):
            append_repodoc(conn, 1, "spec", SHA, {"repo": "r"})


def test_default_subject_is_items_only_and_all_is_both(store):
    """Review finding 5: old clients (no subject) never see a repodoc id as an item_id."""
    from culture_library.core.feed import append_repodoc

    with store.connect() as conn:
        append_repodoc(conn, 1, "spec", SHA, {"repo": "r", "path": "a.md"})
        c2 = append(conn, _ev("created", item_id=1))
        append_repodoc(conn, 2, "plan", SHA, {"repo": "r", "path": "b.md"})
    with store.reader() as conn:
        page = read_since(conn, 0)
        assert [e["cursor"] for e in page["events"]] == [c2]
        assert {e["subject"] for e in page["events"]} == {"item"}
        assert page["cursor"] == c2  # a cursor never skips past an unread item event
        assert [e["subject"] for e in read_since(conn, 0, subject="all")["events"]] == [
            "repodoc",
            "item",
            "repodoc",
        ]
        with pytest.raises(ValidationError):
            read_since(conn, 0, subject="everything")


# ------------------------------------------------------------ cursor order is commit order


def _drain(store, saved):
    """Page the feed from ``saved`` to its end; return ``(events, cursor)``."""
    got = []
    while True:
        with store.reader() as conn:
            page = read_since(conn, saved, limit=7)
        got += page["events"]
        saved = page["cursor"]
        if not page["more"]:
            return got, saved


def test_late_committer_is_not_skipped_by_a_paging_reader(store):
    """Writer A appends and holds its transaction open; writer B then appends and commits.

    Without the append lock B draws the later seq and commits first, so a reader that saves
    B's cursor never sees A's earlier event. With it B waits for A, so cursor order is
    commit order and the reader sees A before B.
    """
    a_appended, a_may_commit = threading.Event(), threading.Event()
    seqs = {}

    def writer_a():
        with store.connect() as conn:
            seqs["a"] = append(conn, _ev("created", 1))
            a_appended.set()
            assert a_may_commit.wait(10)

    def writer_b():
        with store.connect() as conn:
            seqs["b"] = append(conn, _ev("created", 2))

    ta = threading.Thread(target=writer_a)
    ta.start()
    assert a_appended.wait(10)
    tb = threading.Thread(target=writer_b)
    tb.start()
    tb.join(0.5)  # lock-free, B commits within milliseconds; locked, it is still waiting
    seen, cursor = _drain(store, 0)  # the reader pages while A is still open
    a_may_commit.set()
    ta.join(10)
    tb.join(10)
    more, cursor = _drain(store, cursor)
    seen += more
    assert [e["item_id"] for e in seen] == [1, 2]
    assert [e["cursor"] for e in seen] == [seqs["a"], seqs["b"]]
    assert seqs["a"] < seqs["b"]


def test_two_concurrent_writers_and_a_paging_reader_see_every_event_once_in_order(store):
    per_writer = 40
    done = threading.Event()
    errors = []

    def writer(base):
        try:
            for i in range(per_writer):
                with store.connect() as conn:
                    append(conn, _ev("created", base + i))
                    if i % 5 == 0:
                        time.sleep(0.005)  # hold the transaction open to widen the race
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    seen, cursor = [], 0

    def reader():
        nonlocal cursor
        while not done.is_set():
            got, cursor = _drain(store, cursor)
            seen.extend(got)

    threads = [threading.Thread(target=writer, args=(base,)) for base in (0, 1000)]
    rt = threading.Thread(target=reader)
    rt.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    done.set()
    rt.join(60)
    got, cursor = _drain(store, cursor)
    seen.extend(got)
    assert not errors
    ids = [e["item_id"] for e in seen]
    assert sorted(ids) == sorted([*range(per_writer), *range(1000, 1000 + per_writer)])
    cursors = [e["cursor"] for e in seen]
    assert cursors == sorted(cursors)
    assert len(set(cursors)) == len(cursors)


def test_append_lock_is_released_with_the_transaction(store):
    with store.connect() as conn:
        append(conn, _ev("created", 1))
        append(conn, _ev("updated", 1))  # the same transaction re-takes its own lock
    with store.connect() as conn:  # a later transaction is not blocked
        append(conn, _ev("created", 2))
