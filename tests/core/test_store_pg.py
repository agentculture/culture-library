"""Store tests on Postgres (plan library-on-postgres-and-pgvector, task t1).

Dedup/data-dir/kind/validation cases adapted from agentculture/xteink @ ea2f643, path
tests/core/test_library.py (cite-don't-import), via the SQLite-era tests/core/test_store.py;
connection, migration-runner, collation and row tests are new.

Each test gets its own scratch database created from ``CULTURE_LIBRARY_TEST_PG_DSN`` (an
admin DSN, e.g. ``postgresql://postgres@127.0.0.1:55432/postgres``) and dropped afterwards.
t2 replaces this module-local fixture with the shared per-worker template.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg", reason="needs the [pg] extra: uv sync --extra pg")
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402

import culture_library.core  # noqa: E402
from culture_library.core import KINDS, CoreError, Store, ValidationError  # noqa: E402
from culture_library.core import migrations  # noqa: E402
from culture_library.core import store as store_mod  # noqa: E402

ADMIN_ENV = "CULTURE_LIBRARY_TEST_PG_DSN"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def no_network():
    """Override the core suite's socket guard: these tests talk to a real Postgres on purpose.

    (libpq opens its own sockets in C, so the Python-level guard would not see them anyway;
    overriding it says so instead of passing by accident.)
    """
    yield []


@pytest.fixture
def admin_dsn():
    dsn = os.environ.get(ADMIN_ENV)
    if not dsn:
        pytest.fail(f"set {ADMIN_ENV} to an admin DSN of a disposable Postgres 17 to run these")
    return dsn


def _create_db(admin_dsn: str, *, collate: str = "C") -> tuple[str, str]:
    name = f"t1_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            f"CREATE DATABASE {name} TEMPLATE template0 LOCALE_PROVIDER libc"
            f" LC_COLLATE '{collate}' LC_CTYPE '{collate}'"
        )
    return name, make_conninfo(admin_dsn, dbname=name)


def _drop_db(admin_dsn: str, name: str) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def db(admin_dsn):
    """A fresh, empty C-collation database; yields its conninfo."""
    name, dsn = _create_db(admin_dsn)
    yield dsn
    _drop_db(admin_dsn, name)


@pytest.fixture
def store(tmp_path, db):
    return Store(tmp_path / "data", dsn=db)


def _add(store, data=b"hello", **kw):
    meta = dict(title="Dune", author="Frank Herbert", kind="book", format="epub")
    meta.update(kw)
    return store.add_item(data, **meta)


def _raw(dsn: str):
    return psycopg.connect(dsn, autocommit=True)


# --- settings ---------------------------------------------------------------------------------


def test_conninfo_from_parts():
    env = {
        "CULTURE_LIBRARY_PG_HOST": "db.internal",
        "CULTURE_LIBRARY_PG_PORT": "6543",
        "CULTURE_LIBRARY_PG_DATABASE": "lib",
        "CULTURE_LIBRARY_PG_USER": "librarian",
        "CULTURE_LIBRARY_PG_PASSWORD": "s3cret",
    }
    parts = conninfo_to_dict(store_mod.conninfo_from_env(env))
    assert parts == {
        "host": "db.internal",
        "port": "6543",
        "dbname": "lib",
        "user": "librarian",
        "password": "s3cret",
    }


def test_conninfo_defaults_without_settings():
    parts = conninfo_to_dict(store_mod.conninfo_from_env({}))
    assert parts == {
        "host": "127.0.0.1",
        "port": "5432",
        "dbname": "culture_library",
        "user": "culture_library",
    }


def test_dsn_overrides_parts_and_takes_the_password():
    env = {
        "CULTURE_LIBRARY_PG_DSN": "postgresql://u@127.0.0.1:1/d",
        "CULTURE_LIBRARY_PG_HOST": "ignored",
        "CULTURE_LIBRARY_PG_DATABASE": "ignored",
        "CULTURE_LIBRARY_PG_PASSWORD": "pw",
    }
    parts = conninfo_to_dict(store_mod.conninfo_from_env(env))
    assert parts == {"host": "127.0.0.1", "port": "1", "dbname": "d", "user": "u", "password": "pw"}


def test_dsn_with_its_own_password_keeps_it():
    env = {
        "CULTURE_LIBRARY_PG_DSN": "postgresql://u:inline@127.0.0.1/d",
        "CULTURE_LIBRARY_PG_PASSWORD": "other",
    }
    assert conninfo_to_dict(store_mod.conninfo_from_env(env))["password"] == "inline"


def test_store_reads_dsn_from_env(monkeypatch, tmp_path, db):
    monkeypatch.setenv("CULTURE_LIBRARY_PG_DSN", db)
    s = Store(tmp_path / "data")
    with s.reader() as conn:
        assert conn.execute("SELECT current_database()").fetchone()[0] == conninfo_to_dict(db)[
            "dbname"
        ]


def test_data_dir_from_env(monkeypatch, tmp_path, db):
    monkeypatch.setenv("CULTURE_LIBRARY_DATA_DIR", str(tmp_path / "envdata"))
    s = Store(dsn=db)
    assert s.data_dir == tmp_path / "envdata"
    assert (tmp_path / "envdata" / "files").is_dir()


def test_password_not_in_repr(tmp_path, db):
    s = Store(tmp_path / "d", dsn=make_conninfo(db, password="hunter2"))
    assert "hunter2" not in repr(s)
    assert "hunter2" not in str(vars(s).get("data_dir"))


def test_unreachable_database_is_a_core_error(tmp_path):
    with pytest.raises(CoreError, match="CULTURE_LIBRARY_PG_"):
        Store(tmp_path / "d", dsn="postgresql://nobody@127.0.0.1:1/none?connect_timeout=2")


# --- transactions -------------------------------------------------------------------------------


def test_connect_commits_on_success(store, db):
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO items (sha256, kind, title, format, size, created_at)"
            " VALUES ('a', 'book', 't', 'epub', 1, 'now')"
        )
    with _raw(db) as conn:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1


def test_connect_is_one_transaction_rolled_back_on_error(store, db):
    with pytest.raises(RuntimeError):
        with store.connect() as conn:
            conn.execute(
                "INSERT INTO items (sha256, kind, title, format, size, created_at)"
                " VALUES ('a', 'book', 't', 'epub', 1, 'now')"
            )
            conn.execute(
                "INSERT INTO items (sha256, kind, title, format, size, created_at)"
                " VALUES ('b', 'book', 't', 'epub', 1, 'now')"
            )
            raise RuntimeError("boom")
    with _raw(db) as conn:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0


def test_connect_rolls_back_on_sql_error(store, db):
    with pytest.raises(psycopg.errors.CheckViolation):
        with store.connect() as conn:
            conn.execute(
                "INSERT INTO items (sha256, kind, title, format, size, created_at)"
                " VALUES ('a', 'book', 't', 'epub', 1, 'now')"
            )
            conn.execute(
                "INSERT INTO items (sha256, kind, title, format, size, created_at)"
                " VALUES ('b', 'comic', 't', 'epub', 1, 'now')"
            )
    with _raw(db) as conn:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0


def test_connect_is_not_autocommit(store):
    with store.connect() as conn:
        assert conn.autocommit is False
        # Two statements in one transaction see the same transaction start time.
        a = conn.execute("SELECT now()").fetchone()[0]
        b = conn.execute("SELECT now()").fetchone()[0]
        assert a == b


def test_reader_is_read_only_and_refuses_row_locks(store):
    _add(store)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        with store.reader() as conn:
            conn.execute("SELECT * FROM items FOR UPDATE").fetchall()
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        with store.reader() as conn:
            conn.execute("UPDATE items SET title = 'x'")


def test_reader_does_not_block_a_writer(store, db):
    item = _add(store).item
    with store.reader() as reader:
        assert reader.execute("SELECT title FROM items WHERE id = %s", (item.id,)).fetchone()
        # A writer on another connection must not wait on the open reader.
        with _raw(db) as writer:
            writer.execute("SET lock_timeout = '1s'")
            writer.execute("UPDATE items SET title = 'Renamed' WHERE id = %s", (item.id,))
    assert store.get_item(item.id).title == "Renamed"


# --- rows -----------------------------------------------------------------------------------------


def test_rows_support_index_name_and_dict_access(store):
    item = _add(store).item
    with store.reader() as conn:
        row = conn.execute("SELECT id, title, author FROM items").fetchone()
    assert row[0] == item.id
    assert row["title"] == "Dune"
    assert row["TITLE"] == "Dune"  # sqlite3.Row names are case-insensitive
    assert row[-1] == "Frank Herbert"
    assert row[1:] == ("Dune", "Frank Herbert")
    assert list(row.keys()) == ["id", "title", "author"]
    assert dict(row) == {"id": item.id, "title": "Dune", "author": "Frank Herbert"}
    ident, title, author = row
    assert (ident, title, author) == (item.id, "Dune", "Frank Herbert")
    assert len(row) == 3
    with pytest.raises(IndexError):
        row["missing"]


def test_scalar_row_and_no_result(store):
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0
        conn.execute("SELECT 1 WHERE false")


# --- migrations -----------------------------------------------------------------------------------


def _ddl_logger(dsn: str) -> None:
    """Install an event trigger that logs every DDL command the server runs in ``dsn``."""
    with _raw(dsn) as conn:
        conn.execute("CREATE TABLE ddl_log (tag text NOT NULL)")
        conn.execute(
            "CREATE FUNCTION log_ddl() RETURNS event_trigger LANGUAGE plpgsql AS"
            " $$ BEGIN INSERT INTO ddl_log VALUES (tg_tag); END $$"
        )
        conn.execute("CREATE EVENT TRIGGER log_ddl ON ddl_command_start EXECUTE FUNCTION log_ddl()")


def _ddl_count(dsn: str) -> int:
    with _raw(dsn) as conn:
        return conn.execute("SELECT count(*) FROM ddl_log").fetchone()[0]


def test_shipped_migrations_are_numbered_from_zero():
    found = migrations.load()
    assert [m.version for m in found] == list(range(len(found)))
    assert found[0].name == "0000_schema_version"
    assert migrations.CURRENT_VERSION == found[-1].version


def test_first_open_applies_each_migration_once(store, db):
    with _raw(db) as conn:
        rows = conn.execute("SELECT version, name FROM schema_version ORDER BY version").fetchall()
    assert rows == [(m.version, m.name) for m in migrations.load()]


def test_migrate_returns_names_applied_then_nothing(db):
    with psycopg.connect(db) as conn:
        assert migrations.migrate(conn) == [m.name for m in migrations.load()]
    with psycopg.connect(db) as conn:
        assert migrations.migrate(conn) == []


def test_second_open_runs_no_ddl(tmp_path, db, monkeypatch):
    _ddl_logger(db)
    Store(tmp_path / "d", dsn=db)
    after_first = _ddl_count(db)
    assert after_first >= 2  # the bootstrap and items tables at least

    statements: list[str] = []
    real = psycopg.Cursor.execute

    def counting(self, query, *args, **kwargs):
        statements.append(str(query))
        return real(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", counting)
    Store(tmp_path / "d", dsn=db)
    monkeypatch.undo()

    assert _ddl_count(db) == after_first  # server side: no DDL command ran
    ddl_words = ("CREATE", "ALTER", "DROP", "TRUNCATE", "COMMENT", "GRANT")
    assert statements, "the open should still check the schema"
    assert len(statements) <= 3, statements
    assert not [s for s in statements if s.lstrip().upper().startswith(ddl_words)], statements


def test_concurrent_first_opens_apply_once(tmp_path, db):
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def opener():
        try:
            barrier.wait()
            Store(tmp_path / "d", dsn=db)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=opener) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with _raw(db) as conn:
        rows = conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    assert [r[0] for r in rows] == [m.version for m in migrations.load()]


def test_ahead_schema_version_is_refused(store, db, tmp_path):
    with _raw(db) as conn:
        conn.execute(
            "INSERT INTO schema_version (version, name, applied_at) VALUES (%s, %s, 'now')",
            (migrations.CURRENT_VERSION + 1, "9999_from_the_future"),
        )
    with pytest.raises(CoreError, match="newer culture-library") as info:
        Store(tmp_path / "again", dsn=db)
    assert str(migrations.CURRENT_VERSION + 1) in str(info.value)


def test_renamed_migration_is_refused(store, db, tmp_path):
    with _raw(db) as conn:
        conn.execute("UPDATE schema_version SET name = 'renamed' WHERE version = 0")
    with pytest.raises(CoreError, match="renamed"):
        Store(tmp_path / "again", dsn=db)


def test_failed_migration_rolls_back_everything(db, tmp_path):
    bad = tmp_path / "migs"
    bad.mkdir()
    for m in migrations.load():
        (bad / f"{m.name}.sql").write_text(m.sql)
    nxt = migrations.CURRENT_VERSION + 1
    (bad / f"{nxt:04d}_broken.sql").write_text("CREATE TABLE ok_part (x int); SELECT 1/0;")
    with psycopg.connect(db) as conn:
        with pytest.raises(psycopg.errors.DivisionByZero):
            migrations.migrate(conn, migrations.load(bad))
    with _raw(db) as conn:
        assert conn.execute("SELECT to_regclass('schema_version')").fetchone()[0] is None
        assert conn.execute("SELECT to_regclass('ok_part')").fetchone()[0] is None


@pytest.mark.parametrize(
    "files, message",
    [
        (["0000_a.sql", "0000_b.sql"], "duplicate"),
        (["0000_a.sql", "0002_b.sql"], "gap"),
        (["0001_a.sql"], "start at 0000"),
        (["0000_a.sql", "1_b.sql"], "NNNN_name.sql"),
    ],
)
def test_bad_migration_directories_are_refused(tmp_path, files, message):
    for f in files:
        (tmp_path / f).write_text("SELECT 1;")
    with pytest.raises(CoreError, match=message):
        migrations.load(tmp_path)


def test_migrate_refuses_an_autocommit_connection(db):
    with _raw(db) as conn:
        with pytest.raises(CoreError, match="transaction"):
            migrations.migrate(conn)


# --- database settings ----------------------------------------------------------------------------


def test_non_c_collation_is_refused(admin_dsn, tmp_path):
    name, dsn = _create_db(admin_dsn, collate="en_US.utf8")
    try:
        with pytest.raises(CoreError, match="collation") as info:
            Store(tmp_path / "d", dsn=dsn)
        assert "LC_COLLATE 'C'" in str(info.value)
        with _raw(dsn) as conn:  # refused before any DDL ran
            assert conn.execute("SELECT to_regclass('schema_version')").fetchone()[0] is None
    finally:
        _drop_db(admin_dsn, name)


def test_items_kind_check_matches_kinds(store):
    with store.reader() as conn:
        definition = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conrelid = 'items'::regclass AND conname = 'items_kind_check'"
        ).fetchone()[0]
    named = [part.split("'")[1] for part in definition.split("ARRAY[")[1].split(",")]
    assert tuple(named) == KINDS


def test_items_columns(store):
    with store.reader() as conn:
        cols = [
            r["column_name"]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'items' ORDER BY ordinal_position"
            )
        ]
    assert cols == [
        "id", "sha256", "kind", "title", "author", "format", "size",
        "visibility", "created_at", "archived_at",
    ]  # fmt: skip


# --- items CRUD -----------------------------------------------------------------------------------


def test_add_records_metadata_and_blob_layout(store):
    res = _add(store)
    item = res.item
    sha = hashlib.sha256(b"hello").hexdigest()
    assert res.created is True
    assert item.sha256 == sha
    assert (item.kind, item.title, item.author, item.format, item.size) == (
        "book", "Dune", "Frank Herbert", "epub", 5,
    )  # fmt: skip
    assert item.visibility == "private"
    assert item.archived_at is None
    assert store.blob_path(sha) == store.files_dir / sha[:2] / sha
    assert store.read_bytes(item.id) == b"hello"
    assert store.get_item(item.id) == item


def test_get_missing_item(store):
    assert store.get_item(12345) is None
    with pytest.raises(culture_library.core.NotFoundError):
        store.read_bytes(12345)


def test_dedup_by_sha256(store):
    first = _add(store)
    second = _add(store, title="Other title")
    assert second.created is False
    assert second.item == first.item


def test_dedup_does_not_burn_ids(store):
    assert _add(store).item.id == 1
    _add(store)
    assert _add(store, data=b"two").item.id == 2


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


def test_visibility_public(store):
    assert _add(store, visibility="public").item.visibility == "public"


def test_explicit_id_preserved_and_sequence_moves_past_it(store):
    assert _add(store, item_id=42).item.id == 42
    assert _add(store, data=b"next").item.id == 43


def test_explicit_low_id_never_moves_the_sequence_back(store):
    assert _add(store, data=b"a").item.id == 1
    assert _add(store, data=b"b", item_id=50).item.id == 50
    assert _add(store, data=b"c", item_id=7).item.id == 7
    assert _add(store, data=b"d").item.id == 51


def test_add_item_on_insert_runs_in_the_insert_transaction(store):
    seen = []

    def hook(conn, item):
        row = conn.execute("SELECT title FROM items WHERE id = %s", (item.id,)).fetchone()
        seen.append((item.title, row[0]))

    result = store.add_item(b"hooked", kind="book", title="Hooked", format="txt", on_insert=hook)
    assert result.created
    assert seen == [("Hooked", "Hooked")]
    again = store.add_item(b"hooked", kind="book", title="Hooked", format="txt", on_insert=hook)
    assert not again.created
    assert len(seen) == 1  # not called for a duplicate


def test_add_item_on_insert_raising_rolls_back_the_insert(store):
    def boom(conn, item):
        raise RuntimeError("hook failed")

    with pytest.raises(RuntimeError, match="hook failed"):
        store.add_item(b"doomed", kind="book", title="Doomed", format="txt", on_insert=boom)
    with store.reader() as conn:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    assert store.add_item(b"doomed", kind="book", title="Doomed", format="txt").created


def test_concurrent_adds_of_the_same_bytes_create_one_item(store):
    results = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def adder():
        try:
            barrier.wait()
            results.append(_add(store, data=b"same"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=adder) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sum(r.created for r in results) == 1
    assert len({r.item.id for r in results}) == 1


def test_concurrent_writes_and_reads(store):
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer(t):
        try:
            for i in range(50):
                _add(store, data=f"{t}-{i}".encode(), title=f"t{t}-{i}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            while not stop.is_set():
                with store.reader() as conn:
                    conn.execute("SELECT count(*) FROM items").fetchone()
        except BaseException as exc:  # noqa: BLE001
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
    assert errors == []
    with store.reader() as conn:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 200


# --- import hygiene -------------------------------------------------------------------------------

_NO_PSYCOPG = """
import sys

class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in {'psycopg', 'psycopg_binary', 'psycopg_c'}:
            raise ImportError('blocked: ' + name)

sys.meta_path.insert(0, Block())
import culture_library.core.store as store_mod
from culture_library.core import CoreError, migrations
assert migrations.CURRENT_VERSION >= 1
try:
    store_mod.Store(sys.argv[1], dsn='postgresql://x@127.0.0.1:1/x')
except CoreError as exc:
    assert 'culture-library[pg]' in str(exc), exc
    sys.exit(0)
sys.exit(99)
"""


def test_store_imports_without_psycopg(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _NO_PSYCOPG, str(tmp_path / "d")],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT)},
    )
    assert proc.returncode == 0, proc.stderr


def test_migrations_ship_with_the_package():
    pkg = Path(migrations.__file__).parent
    assert sorted(p.name for p in pkg.glob("*.sql"))[:2] == [
        "0000_schema_version.sql",
        "0001_initial.sql",
    ]


def test_core_source_has_no_network_imports():
    banned = ("socket", "urllib", "http", "requests", "httpx", "asyncio")
    core = Path(culture_library.core.__file__).parent
    for py in [*core.glob("*.py"), *core.glob("migrations/*.py")]:
        for line in py.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] in ("import", "from"):
                assert parts[1].split(".")[0] not in banned, (py.name, line)


def test_provenance_headers():
    for name in ("store", "models", "errors"):
        src = (Path(culture_library.core.__file__).parent / f"{name}.py").read_text()
        assert "ea2f643" in src
        assert f"xteink/core/{name}.py" in src
