"""The ``design`` kind: KINDS, kind inference for docs/design/, and the items CHECK rebuild.

The CHECK is Postgres's ``items_kind_check`` (``core/migrations/0001_initial.sql``).
"""

from __future__ import annotations

import psycopg
import pytest

from culture_library.core import KINDS, Store
from culture_library.core.repodocs import RepoDocs, infer_kind


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


def _raw_insert(store: Store, kind: str, sha: str) -> None:
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO items (sha256, kind, title, author, format, size, created_at)"
            " VALUES (%s, %s, 'raw', '', 'md', 1, '2026-01-01T00:00:00+00:00')",
            (sha, kind),
        )


def _check_definition(store: Store) -> str:
    with store.reader() as conn:
        return conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conrelid = 'items'::regclass AND conname = 'items_kind_check'"
        ).fetchone()[0]


def test_items_kind_check_admits_design(store: Store) -> None:
    """The Postgres schema's items_kind_check (0001_initial.sql) names the kind, and a raw
    insert of it passes the CHECK while an unknown kind is refused.

    Replaces the SQLite-only test that built a pre-design database and checked the
    rebuild-on-open widened the CHECK: Postgres has no rebuild on open; a new kind is a
    numbered migration that drops and re-adds items_kind_check (checked against designS by
    tests/core/test_store_pg.py::test_items_kind_check_matches_kinds).
    """
    assert "'design'" in _check_definition(store)
    _raw_insert(store, "design", "a" * 64)
    with pytest.raises(psycopg.errors.CheckViolation):
        _raw_insert(store, "not-a-kind", "b" * 64)
