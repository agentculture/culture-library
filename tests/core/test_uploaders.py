"""item_uploaders on Postgres: record, lookup and the %s-style visibility fragment."""

from culture_library.core import uploaders


def _item(store, data: bytes, **kw):
    return store.add_item(data, kind="document", title="t", format="md", **kw).item


def test_record_lookup_and_replace(store):
    item = _item(store, b"one\n")
    with store.connect() as conn:
        uploaders.record(conn, item.id, "alice")
        assert uploaders.uploader(conn, item.id) == "alice"
        assert uploaders.uploaded_by(conn, item.id, "alice")
        assert not uploaders.uploaded_by(conn, item.id, "bob")
        assert not uploaders.uploaded_by(conn, item.id, None)
        uploaders.record(conn, item.id, "bob")  # a stale row is replaced
        assert uploaders.uploader(conn, item.id) == "bob"
        uploaders.record(conn, item.id, None)  # and cleared
        assert uploaders.uploader(conn, item.id) is None


def test_visible_sql_uses_percent_s_placeholders(store):
    assert uploaders.visible_sql(True) == ("1=1", [])
    assert uploaders.visible_sql(False) == ("visibility = 'public'", [])
    sql, params = uploaders.visible_sql(False, "alice", visibility="i.visibility", item_id="i.id")
    assert "%s" in sql and "?" not in sql
    assert params == ["alice"]


def test_visible_sql_filters_rows(store):
    pub = _item(store, b"pub\n", visibility="public")
    mine = _item(store, b"mine\n", visibility="private")
    theirs = _item(store, b"theirs\n", visibility="private")
    with store.connect() as conn:
        uploaders.record(conn, mine.id, "alice")
        uploaders.record(conn, theirs.id, "bob")

        def ids(include_private, owner):
            cond, params = uploaders.visible_sql(include_private, owner)
            rows = conn.execute(
                f"SELECT id FROM items WHERE {cond} ORDER BY id", params
            )  # noqa: S608
            return [r[0] for r in rows.fetchall()]

        assert ids(False, None) == [pub.id]
        assert ids(False, "alice") == [pub.id, mine.id]
        assert ids(True, None) == [pub.id, mine.id, theirs.id]
        assert uploaders.is_visible(conn, mine.id, "private", False, "alice")
        assert not uploaders.is_visible(conn, theirs.id, "private", False, "alice")
        assert uploaders.is_visible(conn, theirs.id, "private", True)
