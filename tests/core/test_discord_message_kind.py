"""The ``discord-message`` kind (#19): key minting and the items kind CHECK."""

from __future__ import annotations

import json

import psycopg
import pytest

from culture_library.cli import main
from culture_library.core import KINDS, Store


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


def test_items_kind_check_admits_discord_message(store: Store) -> None:
    """The Postgres schema's items_kind_check (0001_initial.sql) names the kind, and a raw
    insert of it passes the CHECK while an unknown kind is refused.

    Replaces the SQLite-only test that built a pre-discord-message database and checked the
    rebuild-on-open widened the CHECK: Postgres has no rebuild on open; a new kind is a
    numbered migration that drops and re-adds items_kind_check (checked against discord-messageS by
    tests/core/test_store_pg.py::test_items_kind_check_matches_kinds).
    """
    assert "'discord-message'" in _check_definition(store)
    _raw_insert(store, "discord-message", "a" * 64)
    with pytest.raises(psycopg.errors.CheckViolation):
        _raw_insert(store, "not-a-kind", "b" * 64)
