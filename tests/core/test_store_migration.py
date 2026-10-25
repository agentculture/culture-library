"""Migration of an existing ``items`` table whose kind CHECK predates the current KINDS.

The old database is built with literal SQL (the 9-kind schema shipped before ``todo``), with
child rows in ``embeddings`` and ``index_queue`` that reference items with ON DELETE CASCADE,
so a rebuild that lets foreign keys fire would visibly lose them.
"""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from culture_library.cli import main
from culture_library.core import KINDS, Store
from culture_library.core import store as store_mod

OLD_KINDS = (
    "book",
    "article",
    "newspaper",
    "document",
    "transcript",
    "diary",
    "spec",
    "plan",
    "devague-record",
)

OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('book', 'article', 'newspaper', 'document', 'transcript',
        'diary', 'spec', 'plan', 'devague-record')),
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    format TEXT NOT NULL,
    size INTEGER NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'public')),
    created_at TEXT NOT NULL,
    archived_at TEXT
);
CREATE TABLE IF NOT EXISTS embeddings (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    chunk_no INTEGER NOT NULL,
    text TEXT NOT NULL,
    vec BLOB NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (item_id, chunk_no)
);
CREATE TABLE IF NOT EXISTS index_queue (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done', 'failed')),
    body_done INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    enqueued_at REAL NOT NULL,
    last_error TEXT
);
"""

ITEM_COLUMNS = [
    "id",
    "sha256",
    "kind",
    "title",
    "author",
    "format",
    "size",
    "visibility",
    "created_at",
    "archived_at",
]


def _raw(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _make_old_db(data_dir, n_items: int = 5, *, purge_last: bool = False):
    """Build an old-schema library.db; return its path."""
    data_dir.mkdir(parents=True, exist_ok=True)
    db = data_dir / "library.db"
    conn = _raw(db)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(OLD_SCHEMA)
    for i in range(1, n_items + 1):
        conn.execute(
            "INSERT INTO items (sha256, kind, title, author, format, size, visibility, "
            "created_at, archived_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{i:064x}",
                OLD_KINDS[i % len(OLD_KINDS)],
                f"Title {i}",
                f"Author {i}",
                "md",
                i * 10,
                "public" if i % 2 else "private",
                f"2026-10-{i:02d}T00:00:00+00:00",
                None if i % 3 else "2026-10-20T00:00:00+00:00",
            ),
        )
        for chunk in range(2):
            conn.execute(
                "INSERT INTO embeddings (item_id, chunk_no, text, vec, model) VALUES (?,?,?,?,?)",
                (i, chunk, f"chunk {i}/{chunk}", bytes([i, chunk]), "m"),
            )
        conn.execute(
            "INSERT INTO index_queue (item_id, state, enqueued_at) VALUES (?, 'done', ?)",
            (i, float(i)),
        )
    if purge_last:
        conn.execute("DELETE FROM items WHERE id = ?", (n_items,))
    conn.close()
    return db


def _snapshot(db):
    conn = sqlite3.connect(db)
    try:
        return {
            "items": conn.execute("SELECT * FROM items ORDER BY id").fetchall(),
            "columns": [r[1] for r in conn.execute("PRAGMA table_info(items)")],
            "embeddings": conn.execute(
                "SELECT * FROM embeddings ORDER BY item_id, chunk_no"
            ).fetchall(),
            "index_queue": conn.execute("SELECT * FROM index_queue ORDER BY item_id").fetchall(),
            "seq": conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'items'").fetchone(),
        }
    finally:
        conn.close()


def _items_sql(db) -> str:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'items'"
        ).fetchone()[0]
    finally:
        conn.close()


def _insert_todo(store: Store, data: bytes = b"todo body"):
    return store.add_item(data, kind="todo", title="A todo", format="md")


# --- the kind itself -------------------------------------------------------------------------


def test_todo_is_a_kind():
    assert "todo" in KINDS


def test_cli_mint_with_todo_kind(tmp_path, capsys):
    rc = main(
        [
            "keys",
            "mint",
            "--name",
            "todos",
            "--scope",
            "write",
            "--kind",
            "todo",
            "--data-dir",
            str(tmp_path),
            "--json",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["api_key"]["kinds"] == ["todo"]


# --- migration of an old database ------------------------------------------------------------


def test_old_db_migrates_and_keeps_rows(tmp_path):
    data = tmp_path / "data"
    db = _make_old_db(data)
    before = _snapshot(db)
    assert "'todo'" not in _items_sql(db)

    store = Store(data)

    assert "'todo'" in _items_sql(db)
    after = _snapshot(db)
    assert after["columns"] == ITEM_COLUMNS == before["columns"]
    assert after["items"] == before["items"]
    assert [r[0] for r in after["items"]] == [1, 2, 3, 4, 5]
    assert after["embeddings"] == before["embeddings"]
    assert len(after["embeddings"]) == 10
    assert after["index_queue"] == before["index_queue"]
    assert len(after["index_queue"]) == 5
    assert _insert_todo(store).item.kind == "todo"


def test_foreign_keys_still_cascade_after_migration(tmp_path):
    data = tmp_path / "data"
    db = _make_old_db(data)
    Store(data)
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM items WHERE id = 1")
        conn.commit()
        assert conn.execute("SELECT count(*) FROM embeddings WHERE item_id = 1").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM index_queue WHERE item_id = 1").fetchone() == (0,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_sqlite_sequence_preserved(tmp_path):
    data = tmp_path / "data"
    db = _make_old_db(data, n_items=5, purge_last=True)
    before = _snapshot(db)
    assert before["seq"] == (5,)
    assert max(r[0] for r in before["items"]) == 4

    store = Store(data)

    assert _snapshot(db)["seq"] == (5,)
    assert _insert_todo(store).item.id == 6


def test_empty_old_db_migrates(tmp_path):
    # Never-used old store: no sqlite_sequence row before, a 0 row after the rebuild.
    data = tmp_path / "data"
    db = _make_old_db(data, n_items=0)
    assert _snapshot(db)["seq"] is None

    store = Store(data)

    assert "'todo'" in _items_sql(db)
    assert _insert_todo(store).item.id == 1


def test_second_open_does_not_rebuild(tmp_path, capsys):
    data = tmp_path / "data"
    db = _make_old_db(data)
    Store(data)
    first = capsys.readouterr().err
    assert first.count("\n") == 1
    sql = _items_sql(db)

    Store(data)

    assert _items_sql(db) == sql
    assert capsys.readouterr().err == ""


def test_fresh_db_does_not_rebuild(tmp_path, capsys):
    Store(tmp_path / "data")
    Store(tmp_path / "data")
    assert capsys.readouterr().err == ""


def test_racing_first_opens_rebuild_once(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    db = _make_old_db(data)
    before = _snapshot(db)
    real_copy = store_mod._copy_items
    calls: list[int] = []
    lock = threading.Lock()

    def counting_copy(conn):
        with lock:
            calls.append(1)
        real_copy(conn)

    monkeypatch.setattr(store_mod, "_copy_items", counting_copy)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def opener():
        try:
            barrier.wait()
            Store(data)
        except BaseException as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=opener) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == []
    assert len(calls) == 1
    assert capsys.readouterr().err.count("\n") == 1
    after = _snapshot(db)
    assert after["items"] == before["items"]
    assert after["embeddings"] == before["embeddings"]
    assert after["index_queue"] == before["index_queue"]


# --- containment -----------------------------------------------------------------------------


def test_lost_row_rolls_back(tmp_path, monkeypatch):
    data = tmp_path / "data"
    db = _make_old_db(data)
    before = _snapshot(db)
    old_sql = _items_sql(db)

    def lossy_copy(conn):
        cols = ", ".join(ITEM_COLUMNS)
        conn.execute(
            f"INSERT INTO items_new ({cols}) SELECT {cols} FROM items WHERE id != 3"  # nosec
        )

    monkeypatch.setattr(store_mod, "_copy_items", lossy_copy)

    with pytest.raises(Exception, match="items"):
        Store(data)

    assert _items_sql(db) == old_sql
    assert "'todo'" not in old_sql
    after = _snapshot(db)
    assert after == before
    conn = sqlite3.connect(db)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()
    assert "items_new" not in names


def test_success_diagnostic_line(tmp_path, capsys):
    data = tmp_path / "data"
    _make_old_db(data, n_items=4)
    Store(data)
    err = capsys.readouterr().err
    lines = err.splitlines()
    assert len(lines) == 1
    line = lines[0]
    assert "devague-record" in line
    assert "todo" in line
    assert "4 rows" in line


def test_newer_db_with_unknown_kind_is_refused_not_narrowed(tmp_path):
    data_dir = tmp_path / "lib"
    db = _make_old_db(data_dir, n_items=1)
    conn = _raw(db)
    sql = _items_sql(db)
    conn.close()
    newer = sql.replace("'devague-record')", "'devague-record', 'todo', 'future-kind')")
    assert newer != sql
    conn = _raw(db)
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute("UPDATE sqlite_master SET sql = ? WHERE type='table' AND name='items'", (newer,))
    conn.execute("PRAGMA writable_schema = OFF")
    conn.close()
    with pytest.raises(store_mod.MigrationError, match="future-kind"):
        Store(data_dir)
    assert _items_sql(db) == newer


def test_unrelated_fk_violation_does_not_block_migration(tmp_path):
    data_dir = tmp_path / "lib"
    db = _make_old_db(data_dir, n_items=2)
    conn = _raw(db)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("CREATE TABLE parent_x (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE child_x (pid INTEGER REFERENCES parent_x(id))")
    conn.execute("INSERT INTO child_x (pid) VALUES (999)")
    conn.close()
    Store(data_dir)
    assert "'todo'" in _items_sql(db)
