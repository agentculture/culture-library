"""The ``design`` kind: KINDS, kind inference for docs/design/, and the items CHECK rebuild.

Mirrors ``test_discord_message_kind.py``: a database written before ``design`` existed is
built by letting Store create its real tables and narrowing the items CHECK back, then the
first open must widen it once with every row (and child row) intact.
"""

from __future__ import annotations

import sqlite3

from culture_library.core import KINDS, Store
from culture_library.core import indexer as _indexer  # noqa: F401  registers index_queue
from culture_library.core import search as _search  # noqa: F401  registers embeddings
from culture_library.core import sources as _sources  # noqa: F401  registers item_sources
from culture_library.core import store as store_mod
from culture_library.core.repodocs import RepoDocs, infer_kind

PRE_DESIGN_KINDS = tuple(k for k in KINDS if k != "design")


def test_design_is_a_kind() -> None:
    assert "design" in KINDS
    assert KINDS.index("design") == len(KINDS) - 1  # appended, older kinds keep their order


def test_docs_design_infers_design() -> None:
    assert infer_kind("docs/design/library-site.md") == "design"
    assert infer_kind("docs/design/reference/home.png") == "design"
    assert infer_kind("docs/design/prototype.html") == "design"
    assert infer_kind("docs/designs.md") == "document"


def test_push_to_docs_design_png_gets_design_kind(store: Store) -> None:
    r = RepoDocs(store).push(
        remote="https://github.com/agentculture/culture-library",
        path="docs/design/x.png",
        commit="a" * 40,
        committed_at="2026-10-30T00:00:00Z",
        default_branch_head="a" * 40,
        content=b"\x89PNG\r\n\x1a\n\x00\x01",
    )
    assert r.doc.kind == "design"


def test_design_item_can_be_added(store: Store) -> None:
    added = store.add_item(b"<html></html>", kind="design", title="proto", format="html")
    assert added.item.kind == "design"


def _items_sql(db) -> str:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'items'"
        ).fetchone()[0]
    finally:
        conn.close()


def _make_pre_design_db(data_dir, n_items: int = 4):
    Store(data_dir)
    db = data_dir / "library.db"
    sql = _items_sql(db)
    old_list = ", ".join(f"'{k}'" for k in PRE_DESIGN_KINDS)
    new_sql = sql.replace(f"kind IN ({', '.join(repr(k) for k in KINDS)})", f"kind IN ({old_list})")
    assert new_sql != sql
    conn = sqlite3.connect(db, isolation_level=None)
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
            (
                f"{i:064x}",
                PRE_DESIGN_KINDS[i % len(PRE_DESIGN_KINDS)],
                f"T{i}",
                i,
                f"2026-10-{i:02d}",
            ),
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
    conn.close()
    return db


def _counts(db):
    conn = sqlite3.connect(db)
    try:
        return {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # nosec B608
            for t in ("items", "embeddings", "index_queue", "item_sources")
        }
    finally:
        conn.close()


def test_pre_design_db_migrates_with_row_counts_unchanged(tmp_path, capsys) -> None:
    data = tmp_path / "data"
    db = _make_pre_design_db(data)
    assert "'design'" not in _items_sql(db)
    conn = sqlite3.connect(db)
    before = conn.execute("SELECT * FROM items ORDER BY id").fetchall()
    conn.close()
    counts = _counts(db)
    assert counts == {"items": 4, "embeddings": 4, "index_queue": 4, "item_sources": 4}
    capsys.readouterr()

    store = Store(data)

    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1 and "design" in lines[0] and "4 rows" in lines[0]
    assert "'design'" in _items_sql(db)
    assert store_mod._KIND_CHECK in " ".join(_items_sql(db).split())
    assert _counts(db) == counts
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT * FROM items ORDER BY id").fetchall() == before
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    added = store.add_item(b"<svg/>", kind="design", title="d", format="svg")
    assert added.item.id == 5
    Store(data)  # a second open does not rebuild again
    assert capsys.readouterr().err == ""
