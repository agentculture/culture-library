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


def _mint_rules(d, capsys):
    argv = ["keys", "mint", "--name", "culture-rules", "--scope", "write", "--kind", "newspaper"]
    argv += ["--kind", "transcript", "--private", "--data-dir", d, "--json"]
    assert main(argv) == 0
    return json.loads(capsys.readouterr().out)["api_key"]["id"]


def test_rotate_then_confirm(tmp_path, capsys) -> None:
    d = str(tmp_path)
    old = _mint_rules(d, capsys)
    assert main(["keys", "rotate", str(old), "--kind", "todo", "--data-dir", d, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["old_id"] == old
    assert out["name"] == "culture-rules"
    assert out["scope"] == ["write"]
    assert out["kinds"] == ["newspaper", "transcript", "todo"]
    assert out["private"] is True
    raw = out["key"]
    assert keys.authenticate(Store(d), raw).id == out["new_id"]
    assert main(["keys", "list", "--data-dir", d, "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)["keys"]
    assert [k["replaces"] for k in listed] == [None, old]
    assert raw not in json.dumps(listed)
    confirm = ["keys", "rotate", str(old), "--confirm", str(out["new_id"]), "--data-dir", d]
    assert main(confirm + ["--json"]) == 0
    done = json.loads(capsys.readouterr().out)
    assert (done["old_id"], done["new_id"]) == (old, out["new_id"])
    assert "key" not in done
    assert keys.list_keys(Store(d))[0].revoked_at is not None


def test_rotate_text_prints_only_the_key_on_stdout(tmp_path, capsys) -> None:
    d = str(tmp_path)
    old = _mint_rules(d, capsys)
    assert main(["keys", "rotate", str(old), "--data-dir", d]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip().startswith("clk_")
    assert len(captured.out.strip().splitlines()) == 1
    assert "--confirm" in captured.err


def test_mint_text_prints_only_the_key_on_stdout(tmp_path, capsys) -> None:
    assert (
        main(["keys", "mint", "--name", "x", "--scope", "read", "--data-dir", str(tmp_path)]) == 0
    )
    captured = capsys.readouterr()
    assert len(captured.out.strip().splitlines()) == 1
    assert "shown once" in captured.err


def test_rotate_refusals_are_user_errors(tmp_path, capsys) -> None:
    d = str(tmp_path)
    old = _mint_rules(d, capsys)
    assert main(["keys", "rotate", "99", "--data-dir", d]) == 1
    capsys.readouterr()
    assert main(["keys", "rotate", str(old), "--confirm", "99", "--data-dir", d, "--json"]) == 1
    assert json.loads(capsys.readouterr().err)["remediation"]
    assert main(["keys", "revoke", "--id", str(old), "--data-dir", d]) == 0
    assert main(["keys", "rotate", str(old), "--data-dir", d]) == 1
    assert (
        main(["keys", "rotate", str(old), "--confirm", "1", "--kind", "todo", "--data-dir", d]) == 1
    )
    assert len(keys.list_keys(Store(d))) == 1


def test_rotate_explain(capsys) -> None:
    assert main(["explain", "keys", "rotate"]) == 0
    assert "--confirm" in capsys.readouterr().out
