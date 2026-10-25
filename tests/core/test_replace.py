"""library.replace(): swap an item's bytes under the same id (index, feed and blob effects)."""

import hashlib
import sqlite3

import pytest

from culture_library.core import feed, library, repodocs, search  # noqa: F401
from culture_library.core.errors import NotFoundError, ValidationError
from culture_library.core.ingest import IngestLimits
from culture_library.core.library import DuplicateContentError


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _add(store, data: bytes, name: str = "a.md", **kw):
    item, created = library.add(store, data, name, **kw)
    assert created
    return item


def _rep(store, item_id, data, name, **kw):
    kw.setdefault("include_private", True)
    return library.replace(store, item_id, data, name, **kw)


def _events(store, item_id=None):
    with store.reader() as conn:
        return feed.read_since(conn, 0, item_id=item_id)["events"]


def _hits(store, word):
    return len(search.fulltext(store, word, include_private=True))


def _queue(store, item_id):
    with store.reader() as conn:
        return conn.execute("SELECT * FROM index_queue WHERE item_id = ?", (item_id,)).fetchone()


def _row(store, item_id):
    with store.reader() as conn:
        return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def test_replace_swaps_bytes_keeping_metadata(store):
    old = b"# Old\n\nalpha text\n"
    item = _add(
        store, old, kind="spec", title="My title", author="Ann", visibility="public", uploader="k1"
    )
    new = b"plain new text\n"
    out = _rep(store, item.id, new, "b.txt")
    assert out.id == item.id
    assert (out.sha256, out.size, out.format) == (_sha(new), len(new), "txt")
    assert (out.title, out.author, out.kind, out.visibility, out.created_at) == (
        "My title",
        "Ann",
        "spec",
        "public",
        item.created_at,
    )
    assert library.uploader_of(store, item.id) == "k1"
    assert store.read_bytes(item.id) == new
    assert library.get(store, item.id, include_private=True) == out


@pytest.mark.parametrize(
    ("data", "name", "code"),
    [
        (b"", "x.md", "empty"),
        (b"x", "x.exe", "unsupported_format"),
        (b"nope", "x.pdf", "bad_magic"),
        (b"{bad", "x.json", "invalid_json"),
        (b"x" * 50, "x.md", "too_large"),
    ],
)
def test_replace_refusals_match_upload_and_change_nothing(store, data, name, code):
    """Same ValidationError code as upload (``code`` is the expected one)."""
    old = b"keep me\n"
    item = _add(store, old)
    before_row, before_feed = tuple(_row(store, item.id)), _events(store)
    limits = IngestLimits(max_bytes=20)
    with pytest.raises(ValidationError) as up:
        library.add(store, data, name, limits=limits)
    with pytest.raises(ValidationError) as rep:
        _rep(store, item.id, data, name, limits=limits)
    assert rep.value.code == up.value.code == code
    assert tuple(_row(store, item.id)) == before_row
    assert _events(store) == before_feed
    assert store.blob_path(_sha(old)).read_bytes() == old
    assert not store.blob_path(_sha(data)).exists()


def test_replace_reindexes_body_and_requeues(store):
    item = _add(store, b"zebraword here\n")
    with store.connect() as conn:
        conn.execute("UPDATE index_queue SET state='done', attempts=3, next_attempt_at=99")
    assert _hits(store, "zebraword") == 1
    _rep(store, item.id, b"giraffeword here\n", "n.md")
    assert _hits(store, "zebraword") == 0
    assert _hits(store, "giraffeword") == 1
    q = _queue(store, item.id)
    assert (q["state"], q["attempts"], q["next_attempt_at"], q["body_done"]) == ("pending", 0, 0, 1)


def test_replace_appends_one_updated_event_and_is_idempotent(store):
    item = _add(store, b"first\n")
    new = b"second\n"
    out = _rep(store, item.id, new, "n.md")
    events = [e for e in _events(store, item.id) if e["type"] == "updated"]
    assert len(events) == 1
    assert events[0]["sha256"] == _sha(new)
    assert events[0]["data"] == {
        "replaced": True,
        "old_sha256": item.sha256,
        "size": len(new),
        "format": "md",
    }
    before = _events(store)
    again = _rep(store, item.id, new, "n.md")
    assert again == out
    assert _events(store) == before


def test_old_blob_unlinked_unless_referenced(store):
    item = _add(store, b"old bytes\n")
    _rep(store, item.id, b"new bytes\n", "n.md")
    assert not store.blob_path(item.sha256).exists()
    assert store.blob_path(_sha(b"new bytes\n")).exists()


def test_old_blob_kept_when_repodoc_version_references_it(store):
    item = _add(store, b"shared bytes\n")
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO repodocs (id, repo, path, kind, created_at, updated_at)"
            " VALUES (1, 'r', 'p.md', 'spec', 't', 't')"
        )
        conn.execute(
            "INSERT INTO repodoc_versions (doc_id, commit_sha, committed_at, visibility, sha256,"
            " size, pushed_at) VALUES (1, ?, 't', 'public', ?, 1, 't')",
            ("c" * 40, item.sha256),
        )
    _rep(store, item.id, b"other bytes\n", "n.md")
    assert store.blob_path(item.sha256).read_bytes() == b"shared bytes\n"


def test_toggle_a_b_a_keeps_blob_and_hash(store):
    a, b = b"bytes A\n", b"bytes B\n"
    item = _add(store, a)
    _rep(store, item.id, b, "n.md")
    assert not store.blob_path(_sha(a)).exists()
    out = _rep(store, item.id, a, "n.md")
    assert out.sha256 == _sha(a)
    assert store.blob_path(_sha(a)).exists()
    assert hashlib.sha256(store.read_bytes(item.id)).hexdigest() == _sha(a)
    assert not store.blob_path(_sha(b)).exists()


def test_missing_new_blob_is_written(store):
    item = _add(store, b"one\n")
    out = _rep(store, item.id, b"two\n", "n.md")
    assert store.blob_path(out.sha256).read_bytes() == b"two\n"


def test_duplicate_of_another_item_raises_before_update(store):
    a = _add(store, b"content a\n", visibility="public")
    b = _add(store, b"content b\n", visibility="private")
    with pytest.raises(DuplicateContentError) as exc:
        _rep(store, a.id, b"content b\n", "n.md")
    assert exc.value.code == "duplicate_content"
    assert exc.value.other_id == b.id
    assert exc.value.other_visibility == "private"
    assert isinstance(exc.value, library.CoreError)
    assert not isinstance(exc.value, sqlite3.IntegrityError)
    assert _row(store, a.id)["sha256"] == a.sha256
    assert store.blob_path(a.sha256).exists()


def test_archived_or_missing_or_invisible_raises_not_found(store):
    item = _add(store, b"x1\n")
    library.archive(store, item.id, include_private=True)
    with pytest.raises(NotFoundError):
        _rep(store, item.id, b"x2\n", "n.md")
    assert _row(store, item.id)["sha256"] == item.sha256
    with pytest.raises(NotFoundError):
        _rep(store, 9999, b"x2\n", "n.md")
    priv = _add(store, b"secret\n", visibility="private")
    with pytest.raises(NotFoundError):
        library.replace(store, priv.id, b"x3\n", "n.md", include_private=False)
    library.replace(store, priv.id, b"x3\n", "n.md", include_private=True)


def _embedded_text(store, item_id):
    with store.reader() as conn:
        rows = conn.execute(
            "SELECT text FROM embeddings WHERE item_id = ? ORDER BY chunk_no", (item_id,)
        ).fetchall()
    return " ".join(r[0] for r in rows)


def _vec(texts):
    return [[1.0, float(len(t)), 0.5] for t in texts]


def test_replace_during_embedding_does_not_keep_old_vectors(store):
    from culture_library.core import indexer as ix

    item = _add(store, b"# Old\n\nalpha text\n")
    fired = []

    def racing_embed(texts):
        if not fired:  # the gateway call is in flight when the bytes are replaced
            fired.append(True)
            _rep(store, item.id, b"# New\n\nomega text\n", "a.md")
        return _vec(texts)

    racing_embed.model = "m"
    assert ix.Indexer(store, embed=racing_embed).run_once() == 0
    assert "alpha" not in _embedded_text(store, item.id)
    assert _queue(store, item.id)["state"] == "pending"
    assert ix.Indexer(store, embed=_vec).run_once() == 1
    assert "omega" in _embedded_text(store, item.id)
    assert _queue(store, item.id)["state"] == "done"


def test_replace_during_body_extraction_keeps_new_body(store):
    from culture_library.core import indexer as ix

    item = _add(store, b"# Old\n\nalpha text\n")
    ix.requeue_all(store)  # body_done=0: the indexer re-extracts the body
    fired = []

    def racing_text_for(it):
        if not fired:
            fired.append(True)
            _rep(store, item.id, b"# New\n\nomega text\n", "a.md")
        return "alpha text"  # what the indexer extracted from the old bytes

    assert ix.Indexer(store, text_for=racing_text_for).run_once() == 0
    assert _hits(store, "omega") == 1
    assert _hits(store, "alpha") == 0
    assert _queue(store, item.id)["state"] == "pending"


def test_replace_clears_old_embeddings(store):
    from culture_library.core import indexer as ix

    item = _add(store, b"# Old\n\nalpha text\n")
    ix.Indexer(store, embed=_vec).run_once()
    assert "alpha" in _embedded_text(store, item.id)
    _rep(store, item.id, b"# New\n\nomega text\n", "a.md")
    assert _embedded_text(store, item.id) == ""


def test_same_bytes_restores_a_missing_blob(store):
    data = b"# Same\n\nbody\n"
    item = _add(store, data)
    store.blob_path(item.sha256).unlink()
    out = _rep(store, item.id, data, "a.md")
    assert out.sha256 == item.sha256
    assert store.blob_path(item.sha256).read_bytes() == data
    assert [e for e in _events(store, item.id) if e["type"] == "updated"] == []


def test_concurrent_replaces_never_leave_a_live_item_without_its_blob(store):
    """c34/h25: writers toggling A -> B -> A on one item, racing each other, while a reader
    checks that the item's current sha256 always has its blob on disk."""
    import threading

    a, b = b"# Toggle\n\nversion a\n", b"# Toggle\n\nversion b\n"
    item = _add(store, a)
    errors: list[str] = []
    stop = threading.Event()

    def writer(first, second):
        try:
            for _ in range(25):
                for data in (first, second):
                    _rep(store, item.id, data, "a.md")
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(f"writer: {exc!r}")

    def reader():
        while not stop.is_set():
            with store.reader() as conn:
                sha = conn.execute("SELECT sha256 FROM items WHERE id = ?", (item.id,)).fetchone()[
                    0
                ]
                if not store.blob_path(sha).exists():
                    # the row is read inside a snapshot; re-check after the snapshot is current
                    row = conn.execute(
                        "SELECT sha256 FROM items WHERE id = ?", (item.id,)
                    ).fetchone()[0]
                    if row == sha and not store.blob_path(sha).exists():
                        errors.append(f"blob missing for live sha {sha[:12]}")

    threads = [
        threading.Thread(target=writer, args=(a, b)),
        threading.Thread(target=writer, args=(b, a)),
    ]
    watch = threading.Thread(target=reader)
    watch.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    watch.join()
    assert errors == []
    final = _row(store, item.id)["sha256"]
    assert store.blob_path(final).exists()
    other = _sha(b) if final == _sha(a) else _sha(a)
    assert not store.blob_path(other).exists()  # the unused version was unlinked


def test_cleanup_failure_after_commit_does_not_fail_the_replace(store, monkeypatch, capsys):
    item = _add(store, b"# Old\n\nalpha\n")

    def broken(store_, shas):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(library, "_unlink_now", broken)
    out = _rep(store, item.id, b"# New\n\nomega\n", "a.md")
    assert out.sha256 == _sha(b"# New\n\nomega\n")
    assert "could not remove" in capsys.readouterr().err


def test_still_referenced_old_blob_takes_no_cleanup_transaction(store, monkeypatch):
    item = _add(store, b"# Old\n\nalpha\n")
    with store.connect() as conn:  # a repo-doc version also names the old bytes
        conn.execute("CREATE TABLE IF NOT EXISTS extra_refs (sha256 TEXT NOT NULL)")
        conn.execute("INSERT INTO extra_refs VALUES (?)", (item.sha256,))
    calls = []
    monkeypatch.setattr(library, "_unlink_now", lambda s, shas: calls.append(shas))
    _rep(store, item.id, b"# New\n\nomega\n", "a.md")
    assert calls == []
    assert store.blob_path(item.sha256).exists()
