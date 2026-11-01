"""The ``todo`` kind and the CLI accepting it.

Kept from the SQLite-era tests/core/test_store_migration.py when t1 (Postgres store) removed
the items kind-CHECK rebuild that file tested; Postgres changes a CHECK with a numbered
migration instead (core/migrations/).
"""

from __future__ import annotations

import json

from culture_library.cli import main
from culture_library.core import KINDS


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
