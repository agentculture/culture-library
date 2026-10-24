"""Tests for the xteink -> culture-library migration (synthetic xteink-shaped data only)."""

import hashlib
import json
import sqlite3

import pytest

from culture_library.cli import main
from culture_library.core import Store
from culture_library.migrate.xteink import MigrationConflict, migrate_from_xteink

IDS = [1, 2, 4, 5, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]


def _title(i: int) -> str:
    if i == 11:
        return "Some Book"
    if i in (2, 4, 5):
        return f"Article {i}"
    if i == 20:
        return "Daily newspaper 2026-10-11 (live test)"
    return f"Daily newspaper 2026-10-{i:02d}"


@pytest.fixture
def xteink(tmp_path):
    src = tmp_path / "xteink"
    (src / "files").mkdir(parents=True)
    conn = sqlite3.connect(src / "library.db")
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT NOT NULL UNIQUE,"
        " kind TEXT NOT NULL, title TEXT NOT NULL, author TEXT NOT NULL DEFAULT '',"
        " format TEXT NOT NULL, size INTEGER NOT NULL, created_at TEXT NOT NULL)"
    )
    for i in IDS:
        data = f"dummy-{i}".encode()
        sha = hashlib.sha256(data).hexdigest()
        (src / "files" / sha[:2]).mkdir(exist_ok=True)
        (src / "files" / sha[:2] / sha).write_bytes(data)
        kind = "book" if i == 11 else "article"
        conn.execute(
            "INSERT INTO items VALUES (?,?,?,?,?,?,?,?)",
            (i, sha, kind, _title(i), "", "epub", len(data), f"2026-10-{i:02d}T00:00:00+00:00"),
        )
    conn.commit()
    conn.close()
    return src


def test_dry_run_changes_nothing(xteink, tmp_path):
    dest = tmp_path / "dest"
    report = migrate_from_xteink(xteink, dest, apply=False)
    assert len(report.rows) == 17
    assert all(r.status == "new" for r in report.rows)
    assert not (dest / "library.db").exists()
    kinds = {r.id: r.new_kind for r in report.rows}
    assert kinds[11] == "book"
    assert kinds[20] == "newspaper"
    assert kinds[2] == "document"
    assert sum(k == "newspaper" for k in kinds.values()) == 13


def test_apply_copies_same_ids_and_sha(xteink, tmp_path):
    dest = tmp_path / "dest"
    report = migrate_from_xteink(xteink, dest, apply=True)
    assert report.matched == 17
    assert report.total == 17
    store = Store(dest)
    for i in IDS:
        item = store.get_item(i)
        assert item is not None
        assert hashlib.sha256(store.read_bytes(i)).hexdigest() == item.sha256
        assert item.visibility == "private"
        assert item.created_at.startswith("2026-10-")


def test_idempotent_and_next_id_above_max(xteink, tmp_path):
    dest = tmp_path / "dest"
    migrate_from_xteink(xteink, dest, apply=True)
    again = migrate_from_xteink(xteink, dest, apply=True)
    assert all(r.status == "unchanged" for r in again.rows)
    assert again.matched == 17
    res = Store(dest).add_item(b"brand new", kind="document", title="n", format="txt")
    assert res.item.id > 20


def test_conflicting_id_is_error_not_overwrite(xteink, tmp_path):
    dest = tmp_path / "dest"
    Store(dest).add_item(b"other", kind="book", title="x", format="epub", item_id=4)
    with pytest.raises(MigrationConflict):
        migrate_from_xteink(xteink, dest, apply=True)
    assert Store(dest).get_item(4).title == "x"


def test_source_opened_read_only(xteink, tmp_path):
    before = (xteink / "library.db").read_bytes()
    migrate_from_xteink(xteink, tmp_path / "d", apply=True)
    assert (xteink / "library.db").read_bytes() == before
    assert not list(xteink.glob("library.db-*"))


def test_cli_dry_run_and_apply_json(xteink, tmp_path, capsys):
    dest = tmp_path / "dest"
    argv = ["migrate", "from-xteink", "--data-dir", str(xteink), "--dest", str(dest), "--json"]
    assert main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["apply"] is False
    assert out["total"] == 17
    assert main(argv + ["--apply", "--visibility", "public"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["matched"] == 17
    assert Store(dest).get_item(1).visibility == "public"


def test_cli_missing_library_db(tmp_path, capsys):
    rc = main(
        ["migrate", "from-xteink", "--data-dir", str(tmp_path), "--dest", str(tmp_path / "d")]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "hint:" in err


def test_cli_conflict_exits_user_error(xteink, tmp_path, capsys):
    dest = tmp_path / "dest"
    Store(dest).add_item(b"other", kind="book", title="x", format="epub", item_id=4)
    rc = main(["migrate", "from-xteink", "--data-dir", str(xteink), "--dest", str(dest), "--apply"])
    assert rc == 1
    assert "hint:" in capsys.readouterr().err


def test_overview_and_explain(capsys):
    assert main(["migrate", "overview", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["subject"].endswith("migrate")
    assert main(["explain", "migrate", "from-xteink"]) == 0


def test_dest_defaults_to_env(xteink, tmp_path, monkeypatch, capsys):
    dest = tmp_path / "envdest"
    monkeypatch.setenv("CULTURE_LIBRARY_DATA_DIR", str(dest))
    assert main(["migrate", "from-xteink", "--data-dir", str(xteink), "--apply"]) == 0
    assert Store(dest).get_item(20) is not None
