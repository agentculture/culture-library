"""Blob references and the advisory-locked unlink (plan task t5)."""

import hashlib
import threading

from culture_library.core import library


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_blob_tables_match_the_schema(store):
    """BLOB_TABLES lists every table with a sha256 column except the feed log."""
    with store.reader() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND column_name = 'sha256'"
        ).fetchall()
    assert {r[0] for r in rows} - {"feed"} == set(library.BLOB_TABLES)


def test_blob_referenced_by_each_listed_table(store):
    item = store.add_item(b"ref bytes\n", kind="document", title="t", format="md").item
    with store.connect() as conn:
        assert library._blob_referenced(conn, item.sha256)
        assert not library._blob_referenced(conn, _sha(b"nothing"))
        conn.execute(
            "INSERT INTO mirror_outbox (key, sha256, enqueued_at) VALUES ('k', %s, 't')",
            (_sha(b"outbox only"),),
        )
        assert library._blob_referenced(conn, _sha(b"outbox only"))


def test_unlink_waits_for_a_writer_and_keeps_the_blob_its_row_names(store):
    """A writer holds the blob's lock and has an uncommitted row naming it; the unlinker blocks,
    then sees the committed row and keeps the file."""
    data = b"racing bytes\n"
    sha = store.write_blob(data)  # the file exists, no row names it yet
    started, finished = threading.Event(), threading.Event()

    def unlinker():
        started.set()
        library._unlink_now(store, [sha])
        finished.set()

    with store.connect() as writer:
        library.lock_blob(writer, sha)
        writer.execute(
            "INSERT INTO items (sha256, kind, title, format, size, created_at) "
            "VALUES (%s, 'document', 't', 'md', %s, 't')",
            (sha, len(data)),
        )
        thread = threading.Thread(target=unlinker)
        thread.start()
        started.wait(5)
        assert not finished.wait(0.5)  # blocked on the writer's lock
    thread.join(10)
    assert finished.is_set()
    assert store.blob_path(sha).exists()


def test_concurrent_unlink_never_deletes_a_referenced_blob(store):
    data = b"toggled bytes\n"
    sha = _sha(data)
    for _ in range(25):
        store.write_blob(data)
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def unlink():
            try:
                barrier.wait(5)
                library._unlink_now(store, [sha])
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        def add():
            try:
                barrier.wait(5)
                library.add(store, data, "t.md")
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=unlink), threading.Thread(target=add)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert errors == []
        with store.reader() as conn:
            referenced = conn.execute("SELECT 1 FROM items WHERE sha256 = %s", (sha,)).fetchone()
        assert referenced is not None
        assert store.blob_path(sha).exists()
        with store.connect() as conn:  # reset for the next round
            conn.execute("DELETE FROM items WHERE sha256 = %s", (sha,))
            conn.execute("DELETE FROM feed")
            conn.execute("DELETE FROM item_uploaders")
        store.blob_path(sha).unlink(missing_ok=True)


def test_purge_expired_unlinks_only_unreferenced_blobs(store):
    from datetime import UTC, datetime, timedelta

    a, _ = library.add(store, b"purge me\n", "a.md")
    b, _ = library.add(store, b"keep me\n", "b.md")
    library.archive(store, a.id, include_private=True)
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO mirror_outbox (key, sha256, enqueued_at) VALUES ('k', %s, 't')",
            (a.sha256,),
        )
    later = datetime.now(UTC) + timedelta(days=31)
    assert library.purge_expired(store, now=later) == [a.id]
    assert store.blob_path(a.sha256).exists()  # the outbox still names it
    assert store.blob_path(b.sha256).exists()
    with store.connect() as conn:
        conn.execute("DELETE FROM mirror_outbox")
    library.archive(store, b.id, include_private=True)
    assert library.purge_expired(store, now=later) == [b.id]
    assert not store.blob_path(b.sha256).exists()


def test_file_changed_at_reads_replace_events(store):
    item, _ = library.add(store, b"v1 text\n", "a.md")
    other, _ = library.add(store, b"other text\n", "o.md")
    assert library.file_changed_at(store, [item.id, other.id]) == {}
    library.replace(store, item.id, b"v2 text\n", "a.md", include_private=True)
    changed = library.file_changed_at(store, [item.id, other.id])
    assert set(changed) == {item.id}
    assert library.file_changed_at(store, []) == {}


def test_list_items_filters_and_visibility(store):
    pub, _ = library.add(store, b"100% a_b\n", "p.md", title="100% sure", visibility="public")
    library.add(store, b"hidden\n", "h.md", title="Hidden", visibility="private", uploader="u1")
    seen = lambda **kw: [i.title for i in library.list_items(store, **kw)]  # noqa: E731
    assert seen() == ["100% sure"]
    assert seen(include_private=True) == ["100% sure", "Hidden"]
    assert seen(owner="u1") == ["100% sure", "Hidden"]
    assert seen(owner="u2") == ["100% sure"]
    assert seen(q="100%") == ["100% sure"]
    assert seen(q="1_0") == []  # underscore is literal, not a wildcard
    assert seen(include_private=True, kind="document", newest_first=True, limit=1) == ["Hidden"]
    assert library.get(store, pub.id).title == "100% sure"
