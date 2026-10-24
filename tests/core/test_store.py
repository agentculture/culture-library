"""Store tests. Dedup/data-dir/kind/validation cases adapted from agentculture/xteink @ ea2f643,
path tests/core/test_library.py (cite-don't-import); concurrency and schema tests are new."""

import hashlib
import sqlite3
import threading
from pathlib import Path

import pytest

import culture_library.core
from culture_library.core import KINDS, Store, ValidationError, register_schema


def _add(store, data=b"hello", **kw):
    meta = dict(title="Dune", author="Frank Herbert", kind="book", format="epub")
    meta.update(kw)
    return store.add_item(data, **meta)


def test_data_dir_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CULTURE_LIBRARY_DATA_DIR", str(tmp_path / "envdata"))
    s = Store()
    assert s.data_dir == tmp_path / "envdata"
    assert (tmp_path / "envdata" / "files").is_dir()


def test_wal_and_busy_timeout(store):
    with store.reader() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000


def test_items_columns(store):
    with store.reader() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(items)")]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert cols == [
        "id", "sha256", "kind", "title", "author", "format", "size",
        "visibility", "created_at", "archived_at",
    ]  # fmt: skip
    assert not tables & {"devices", "queue", "api_keys", "firmware"}


def test_add_records_metadata_and_blob_layout(store):
    res = _add(store)
    item = res.item
    sha = hashlib.sha256(b"hello").hexdigest()
    assert res.created is True and item.sha256 == sha
    assert (item.kind, item.title, item.author, item.format, item.size) == (
        "book", "Dune", "Frank Herbert", "epub", 5,
    )  # fmt: skip
    assert store.blob_path(sha) == store.files_dir / sha[:2] / sha
    assert store.read_bytes(item.id) == b"hello"


def test_insert_without_visibility_is_private(store):
    assert _add(store).item.visibility == "private"
    with store.connect() as conn:  # raw SQL insert also defaults
        conn.execute(
            "INSERT INTO items (sha256, kind, title, format, size, created_at)"
            " VALUES ('x', 'book', 't', 'epub', 1, 'now')"
        )
        assert conn.execute("SELECT visibility FROM items WHERE sha256='x'").fetchone()[0] == (
            "private"
        )


def test_dedup_by_sha256(store):
    first = _add(store)
    second = _add(store, title="Other title")
    assert second.created is False and second.item.id == first.item.id


@pytest.mark.parametrize("kind", KINDS)
def test_all_kinds_accepted(store, kind):
    assert _add(store, data=kind.encode(), kind=kind).item.kind == kind


def test_validation(store):
    with pytest.raises(ValidationError):
        _add(store, kind="comic")
    with pytest.raises(ValidationError):
        _add(store, title="  ")
    with pytest.raises(ValidationError):
        _add(store, visibility="secret")


def test_explicit_id_preserved(store):
    assert _add(store, item_id=42).item.id == 42
    assert _add(store, data=b"next").item.id == 43


def test_register_schema_hook(tmp_path):
    ddl = "CREATE TABLE IF NOT EXISTS feed_probe (id INTEGER PRIMARY KEY, note TEXT)"
    register_schema(ddl)
    register_schema(ddl)
    s = Store(tmp_path / "d")
    with s.reader() as conn:
        conn.execute("SELECT * FROM feed_probe")


def test_concurrent_writes_and_reads_no_lock_errors(store):
    errors: list[Exception] = []
    stop = threading.Event()

    def writer(t):
        try:
            for i in range(500):
                _add(store, data=f"{t}-{i}".encode(), title=f"t{t}-{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            while not stop.is_set():
                with store.reader() as conn:
                    conn.execute("SELECT count(*) FROM items").fetchone()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(2)]
    writers = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join()
    stop.set()
    for t in readers:
        t.join()
    assert not [e for e in errors if isinstance(e, sqlite3.OperationalError)], errors
    assert errors == []
    with store.reader() as conn:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 2000


def test_core_source_has_no_network_imports():
    banned = ("socket", "urllib", "http", "requests", "httpx", "asyncio")
    for py in Path(culture_library.core.__file__).parent.glob("*.py"):
        for line in py.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] in ("import", "from"):
                assert parts[1].split(".")[0] not in banned, (py.name, line)


def test_provenance_headers():
    for name in ("store", "models", "errors"):
        src = (Path(culture_library.core.__file__).parent / f"{name}.py").read_text()
        assert "ea2f643" in src and f"xteink/core/{name}.py" in src


def test_add_item_on_insert_runs_in_the_insert_transaction(tmp_path):
    store = Store(tmp_path / "hook")
    seen = []

    def hook(conn, item):
        row = conn.execute("SELECT title FROM items WHERE id = ?", (item.id,)).fetchone()
        seen.append((item.title, row[0]))

    result = store.add_item(b"hooked", kind="book", title="Hooked", format="txt", on_insert=hook)
    assert result.created and seen == [("Hooked", "Hooked")]
    again = store.add_item(b"hooked", kind="book", title="Hooked", format="txt", on_insert=hook)
    assert not again.created and len(seen) == 1  # not called for a duplicate


def test_add_item_on_insert_raising_rolls_back_the_insert(tmp_path):
    store = Store(tmp_path / "hook")

    def boom(conn, item):
        raise RuntimeError("hook failed")

    with pytest.raises(RuntimeError, match="hook failed"):
        store.add_item(b"doomed", kind="book", title="Doomed", format="txt", on_insert=boom)
    with store.reader() as conn:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    assert store.add_item(b"doomed", kind="book", title="Doomed", format="txt").created
