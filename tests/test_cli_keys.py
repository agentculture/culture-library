"""``culture-library keys``: mint the first admin key from the CLI."""

from __future__ import annotations

import json

from culture_library.cli import main
from culture_library.core import Store, keys


def test_mint_list_revoke(tmp_path, capsys) -> None:
    d = str(tmp_path)
    assert (
        main(["keys", "mint", "--name", "ops", "--scope", "admin", "--data-dir", d, "--json"]) == 0
    )
    out = json.loads(capsys.readouterr().out)
    raw = out["key"]
    assert raw.startswith("clk_")
    assert out["api_key"]["scopes"] == ["admin"]
    assert keys.authenticate(Store(d), raw).key_id == out["api_key"]["key_id"]
    assert main(["keys", "list", "--data-dir", d, "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)["keys"]
    assert len(listed) == 1
    assert "key_hash" not in listed[0]
    assert raw not in json.dumps(listed)
    assert main(["keys", "revoke", "--id", str(listed[0]["id"]), "--data-dir", d]) == 0
    assert "revoked" in capsys.readouterr().out


def test_mint_rejects_bad_kind(tmp_path, capsys) -> None:
    rc = main(
        [
            "keys",
            "mint",
            "--name",
            "x",
            "--scope",
            "write",
            "--kind",
            "nope",
            "--data-dir",
            str(tmp_path),
        ]
    )
    assert rc == 1


def test_revoke_unknown_is_user_error(tmp_path) -> None:
    assert main(["keys", "revoke", "--id", "99", "--data-dir", str(tmp_path)]) == 1


def test_explain_and_overview(capsys) -> None:
    assert main(["explain", "keys", "mint"]) == 0
    assert "first admin key" in capsys.readouterr().out
    assert main(["keys", "overview", "--json"]) == 0
