"""The ``discord-message`` kind (#19): key minting and migration from the pre-#19 schema."""

from __future__ import annotations

import json
import sqlite3

from culture_library.cli import main
from culture_library.core import KINDS, Store
from culture_library.core import store as store_mod

PRE_19_KINDS = tuple(k for k in KINDS if k != "discord-message")


def test_discord_message_is_a_kind():
    assert "discord-message" in KINDS
    assert "discord-react" not in KINDS


def test_cli_mint_with_discord_message_kind(tmp_path, capsys):
    rc = main(
        [
            "keys",
            "mint",
            "--name",
            "culture-rules",
            "--scope",
            "write",
            "--kind",
            "discord-message",
            "--data-dir",
            str(tmp_path),
            "--json",
        ]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["api_key"]["kinds"] == ["discord-message"]


def _items_sql(db) -> str:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'items'"
        ).fetchone()[0]
    finally:
        conn.close()


def _make_pre_19_db(data_dir, n_items: int = 4):
    """The current schema (todo included) as it stood before ``discord-message``.

    Built by letting Store create its real tables, then narrowing the items CHECK back to the
    previous kinds and filling every child table, so the migration has to carry them all.
    """
    Store(data_dir)
    db = data_dir / "library.db"
    conn = sqlite3.connect(db, isolation_level=None)
    sql = _items_sql(db)
    old_list = ", ".join(f"'{k}'" for k in PRE_19_KINDS)
    new_sql = sql.replace(f"kind IN ({', '.join(repr(k) for k in KINDS)})", f"kind IN ({old_list})")
    assert new_sql != sql
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute("UPDATE sqlite_master SET sql = ? WHERE type='table' AND name='items'", (new_sql,))
    conn.execute("PRAGMA writable_schema = OFF")
    conn.close()
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    for i in range(1, n_items + 1):
        conn.execute(
            "INSERT INTO items (sha256, kind, title, author, format, size, visibility, "
            "created_at) VALUES (?, ?, ?, '', 'md', ?, 'private', ?)",
            (f"{i:064x}", PRE_19_KINDS[i % len(PRE_19_KINDS)], f"T{i}", i, f"2026-10-{i:02d}"),
        )
        conn.execute(
            "INSERT INTO embeddings (item_id, chunk_no, text, vec, model) VALUES (?,0,'c',?,'m')",
            (i, bytes([i])),
        )
        conn.execute(
            "INSERT INTO index_queue (item_id, state, enqueued_at) VALUES (?,'done',1)", (i,)
        )
        conn.execute(
            "INSERT INTO item_sources (item_id, manifest_json, updated_at) VALUES (?, '[]', 't')",
            (i,),
        )
        conn.execute(
            "INSERT INTO eval_sets (item_id, item_sha256, chunker_json, adapter, adapter_version,"
            " generator_json, created_at) VALUES (?, 's', '{}', 'a', '1', '{}', 't')",
            (i,),
        )
    conn.close()
    return db


def _counts(db):
    conn = sqlite3.connect(db)
    try:
        return {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # nosec B608
            for t in ("items", "embeddings", "index_queue", "item_sources", "eval_sets")
        }
    finally:
        conn.close()


def test_pre_19_db_migrates_and_keeps_every_row(tmp_path):
    data = tmp_path / "data"
    db = _make_pre_19_db(data)
    assert "'discord-message'" not in _items_sql(db)
    conn = sqlite3.connect(db)
    before = conn.execute("SELECT * FROM items ORDER BY id").fetchall()
    conn.close()
    counts = _counts(db)
    assert counts == {
        "items": 4,
        "embeddings": 4,
        "index_queue": 4,
        "item_sources": 4,
        "eval_sets": 4,
    }

    store = Store(data)

    assert "'discord-message'" in _items_sql(db)
    assert _counts(db) == counts
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT * FROM items ORDER BY id").fetchall() == before
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    added = store.add_item(b"hello", kind="discord-message", title="msg", format="md")
    assert added.item.kind == "discord-message"
    assert added.item.id == 5


def test_pre_19_migration_is_announced_once(tmp_path, capsys):
    data = tmp_path / "data"
    _make_pre_19_db(data, n_items=2)
    capsys.readouterr()
    Store(data)
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    assert "discord-message" in lines[0]
    assert "2 rows" in lines[0]
    Store(data)
    assert capsys.readouterr().err == ""
    assert store_mod._KIND_CHECK in " ".join(_items_sql(data / "library.db").split())
