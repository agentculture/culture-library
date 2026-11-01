"""item_sources on Postgres: jsonb manifests, replace, cascade."""

from culture_library.core import sources


def _item(store):
    return store.add_item(b"src\n", kind="newspaper", title="t", format="md").item


def test_put_get_replace_and_meta(store):
    item = _item(store)
    manifest = [{"article_title": "A", "source_urls": ["http://x"], "excerpt": "e"}]
    with store.connect() as conn:
        assert sources.get(conn, item.id) is None
        assert sources.get_with_meta(conn, item.id) is None
        sources.put(conn, item.id, manifest)
        assert sources.get(conn, item.id) == manifest
        sources.put(conn, item.id, [])
        value, updated_at = sources.get_with_meta(conn, item.id)
    assert value == []
    assert isinstance(updated_at, str)
    assert updated_at


def test_manifest_cascades_with_its_item(store):
    item = _item(store)
    with store.connect() as conn:
        sources.put(conn, item.id, {"k": 1})
        conn.execute("DELETE FROM items WHERE id = %s", (item.id,))
        assert sources.get(conn, item.id) is None
