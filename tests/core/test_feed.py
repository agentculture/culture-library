from __future__ import annotations

import pytest

from culture_library.core import ValidationError
from culture_library.core.feed import EVENT_TYPES, append, read_since

SHA = "a" * 64


def _ev(type_, item_id=1, **kw):
    return {"type": type_, "item_id": item_id, "sha256": SHA, "kind": "book", **kw}


def test_append_all_types_monotonic(store):
    cursors = []
    with store.connect() as conn:
        for t in EVENT_TYPES:
            cursors.append(append(conn, _ev(t)))
    assert set(EVENT_TYPES) == {"created", "updated", "archived", "purged", "send_requested"}
    assert cursors == sorted(cursors) and len(set(cursors)) == len(cursors)


def test_rejects_bad_type(store):
    with store.connect() as conn:
        with pytest.raises(ValidationError):
            append(conn, _ev("exploded"))


def test_event_fields_and_data(store):
    with store.connect() as conn:
        append(conn, _ev("send_requested", visibility="public", data={"target": "xteink"}))
    with store.reader() as conn:
        (e,) = read_since(conn, 0)["events"]
    assert e["type"] == "send_requested" and e["item_id"] == 1 and e["sha256"] == SHA
    assert e["kind"] == "book" and e["visibility"] == "public" and e["at"]
    assert e["data"] == {"target": "xteink"}


def test_append_rolls_back_with_caller_txn(store):
    with pytest.raises(RuntimeError):
        with store.connect() as conn:
            append(conn, _ev("created"))
            raise RuntimeError("boom")
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
        conn.set_trace_callback(queries.append)
        out = read_since(conn, saved)
    assert [e["cursor"] for e in out["events"]] == mix
    assert [e["type"] for e in out["events"]] == ["created", "updated", "archived", "purged"]
    assert out["cursor"] == mix[-1] and out["more"] is False
    assert queries and not any("items" in q.lower() for q in queries)
    with store.reader() as conn:
        assert read_since(conn, out["cursor"])["events"] == []


def test_limit_and_more(store):
    with store.connect() as conn:
        for i in range(5):
            append(conn, _ev("created", i))
    with store.reader() as conn:
        a = read_since(conn, 0, limit=3)
        b = read_since(conn, a["cursor"], limit=3)
    assert len(a["events"]) == 3 and a["more"] is True
    assert len(b["events"]) == 2 and b["more"] is False


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
        assert page["more"] is False and page["cursor"] == page["events"][-1]["cursor"]
        first = read_since(conn, 0, item_id=1, limit=2)
        assert len(first["events"]) == 2 and first["more"] is True
        rest = read_since(conn, first["cursor"], item_id=1)
        assert len(rest["events"]) == 1 and rest["more"] is False
        assert read_since(conn, 0, item_id=99) == {"events": [], "cursor": 0, "more": False}
        # the query read_since actually runs is answered from the feed(item_id) index
        traced: list[str] = []
        conn.set_trace_callback(traced.append)
        read_since(conn, 0, item_id=1)
        conn.set_trace_callback(None)
        sql = next(t for t in traced if "FROM feed" in t)
        plan = " ".join(str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
        assert "USING INDEX feed_item_id" in plan or "USING COVERING INDEX feed_item_id" in plan


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
