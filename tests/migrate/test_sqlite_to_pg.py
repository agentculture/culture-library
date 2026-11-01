"""The one-shot SQLite (0.19.8) -> Postgres copy (plan task t14).

The source is built from the frozen 0.19.8 schema (tests/migrate/sqlite_source.py); the gateway
is a local stub whose ``/capabilities`` answers and whose ``/v1/embeddings`` fails the test.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
import pytest

from culture_library.cli import main
from culture_library.core.embedding_strategy import CURRENT_MODEL_ID, CURRENT_STRATEGY
from culture_library.core.search import EmbeddingError
from culture_library.migrate import sqlite_to_pg as s2p
from tests.migrate import sqlite_source as fx

# Placeholder passwords are generated per run: none is a real credential, and a literal
# would only trip secret scanners.
ENV_PW = secrets.token_hex(8)
INLINE_PW = secrets.token_hex(8)
OTHER_PW = secrets.token_hex(8)

CAPS = {
    "gemma2-embed": {
        "model": CURRENT_MODEL_ID,
        "dimension": 768,
        "normalization": "l2",
    }
}


class _Gateway:
    def __init__(self, caps: dict | None = CAPS):
        self.caps = caps
        self.embed_calls = 0
        self.caps_calls = 0
        gw = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def do_GET(self):  # noqa: N802
                gw.caps_calls += 1
                if self.path != "/capabilities" or gw.caps is None:
                    self.send_response(503)
                    self.end_headers()
                    return
                body = json.dumps(gw.caps).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                gw.embed_calls += 1  # the copy must never embed
                self.send_response(500)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True)
        self.thread.start()

    def embedder(self):
        return s2p.embedder_from_env(url=self.url, model="gemma2-embed")

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.delenv("LOBES_EMBED_PROMPTS", raising=False)
    gw = _Gateway()
    yield gw
    gw.close()
    assert gw.embed_calls == 0, "the copy made an embedding call"


@pytest.fixture
def source(tmp_path):
    return fx.build_source(tmp_path / "library.db")


def _pg(pg_dsn):
    return psycopg.connect(pg_dsn, autocommit=True)


def _library_rows(pg_dsn) -> int:
    with _pg(pg_dsn) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                " AND tablename <> 'schema_version'"
            )
        ]
        return sum(conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables)


def test_copy_covers_every_target_table(pg_dsn):
    with _pg(pg_dsn) as conn:
        tables = {
            r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        }
    assert tables - {"schema_version"} == set(s2p.COPY_ORDER)
    assert set(s2p.ID_COLUMN) == set(s2p.COPY_ORDER)


def test_fixture_source_has_rows_in_every_table(source):
    conn = sqlite3.connect(source)
    for table in s2p.COPY_ORDER:
        name = s2p.SOURCE_TABLE.get(table, table)
        assert conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] > 0, name


def test_copies_every_table_with_ids_and_reports_both_sides(source, pg_dsn, gateway):
    report = s2p.copy_library(source, pg_dsn, embedder=gateway.embedder())
    assert report.status == "copied", report.render()
    assert report.ok
    assert not report.problems
    assert gateway.caps_calls >= 1
    by = {t.table: t for t in report.tables}
    assert set(by) == set(s2p.COPY_ORDER)
    for t in report.tables:
        assert (t.source_count, t.source_max_id) == (t.target_count, t.target_max_id), t
    assert by["items"].source_max_id == 5
    assert by["feed"].target_count == 4
    assert by["item_text"].source_table == "search_fts"
    assert by["item_text"].digest_ok
    assert by["embeddings"].digest_ok
    assert by["repodoc_embeddings"].digest_ok
    with _pg(pg_dsn) as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM items ORDER BY id")] == [1, 2, 5]
        assert conn.execute("SELECT seq, subject FROM feed WHERE seq = 3").fetchone() == (
            3,
            "repodoc",
        )
        assert conn.execute("SELECT private FROM api_keys WHERE id = 1").fetchone()[0] is True
        assert conn.execute("SELECT replaces FROM api_keys WHERE id = 2").fetchone()[0] == 1
        assert conn.execute("SELECT data FROM feed WHERE seq = 1").fetchone()[0] == {
            "n": 1,
            "path": "a/b",
        }
        assert conn.execute("SELECT running FROM eval_runs").fetchone()[0] is False
        assert conn.execute(
            "SELECT on_default_branch FROM repodoc_versions WHERE id = 1"
        ).fetchone()[0]
        assert conn.execute("SELECT body_done FROM index_queue WHERE item_id = 1").fetchone()[0]
        # repodoc_dirty is the SQLite marks exactly, not the triggers' marks for every doc
        assert [r[0] for r in conn.execute("SELECT doc_id FROM repodoc_dirty ORDER BY 1")] == [
            2,
            40,
        ]


def test_full_text_bodies_are_copied_and_searchable(source, pg_dsn, gateway):
    s2p.copy_library(source, pg_dsn, embedder=gateway.embedder())
    with _pg(pg_dsn) as conn:
        title, body = conn.execute("SELECT title, body FROM item_text WHERE item_id = 1").fetchone()
        assert title == "Title 1 Ann"
        assert body == "Café prose.\n\nIt names culture_library/core/search.py and\ttabs."
        hit = conn.execute(
            "SELECT item_id FROM item_text WHERE tsv @@ plainto_tsquery('english', 'search')"
        ).fetchall()
        assert hit == [(1,)]
        assert conn.execute(
            "SELECT title, body, label, visibility FROM repodoc_text WHERE doc_id = 1"
        ).fetchone() == ("Spec one docs/specs/s1.md", "# Spec one\n\nbody", "Spec one", "private")


def test_vectors_are_byte_identical_with_model_id_and_strategy(source, pg_dsn, gateway):
    s2p.copy_library(source, pg_dsn, embedder=gateway.embedder())
    src = sqlite3.connect(source)
    want = {
        (i, n): (blob, label)
        for i, n, blob, label in src.execute("SELECT item_id, chunk_no, vec, model FROM embeddings")
    }
    with _pg(pg_dsn) as conn:
        with conn.cursor(binary=True) as cur:
            cur.execute("SELECT item_id, chunk_no, model_id, strategy, vec FROM embeddings")
            got = cur.fetchall()
        # text form too: the subnormal and -0.0 survive (no decimal round trip on the way in)
        first = conn.execute(
            "SELECT vec::text FROM embeddings WHERE item_id = 1 AND chunk_no = 0"
        ).fetchone()[0]
    assert len(got) == len(want) == 3
    for item_id, chunk_no, model_id, strategy, vec in got:
        assert model_id == CURRENT_MODEL_ID
        assert strategy == CURRENT_STRATEGY.key()
        assert s2p._blob_from_pg(vec) == want[(item_id, chunk_no)][0]
    vals = array("f")
    vals.frombytes(want[(1, 0)][0])
    assert str(vals[0]) == "-0.0"
    assert vals[1] > 0
    assert first.startswith("[-0,1e-45,")


def test_identity_sequences_move_above_copied_and_autoincrement_ids(source, pg_dsn, gateway):
    report = s2p.copy_library(source, pg_dsn, embedder=gateway.embedder())
    # items: max id 5, but SQLite's AUTOINCREMENT had handed out 7; feed had handed out 9
    assert report.sequences["items"] == 8
    assert report.sequences["feed"] == 10
    with _pg(pg_dsn) as conn:
        new_item = conn.execute(
            "INSERT INTO items (sha256, kind, title, format, size, created_at)"
            " VALUES ('n', 'book', 'n', 'txt', 1, 'x') RETURNING id"
        ).fetchone()[0]
        assert new_item == 8
        assert (
            conn.execute(
                "INSERT INTO projects (repo, updated_at) VALUES ('new', 'x') RETURNING id"
            ).fetchone()[0]
            == 4
        )
        seq = conn.execute(
            "INSERT INTO feed (type, item_id, sha256, kind, at) VALUES ('a', 1, 's', 'b', 'x')"
            " RETURNING seq"
        ).fetchone()[0]
        assert seq == 10


def test_empty_table_identity_starts_at_one(tmp_path, pg_dsn, gateway):
    fx.create_schema(tmp_path / "empty.db").close()
    report = s2p.copy_library(tmp_path / "empty.db", pg_dsn, embedder=gateway.embedder())
    assert report.status == "copied", report.render()
    assert all(t.source_count == t.target_count == 0 for t in report.tables)
    assert report.sequences["items"] == 1
    with _pg(pg_dsn) as conn:
        conn.execute(
            "INSERT INTO items (sha256, kind, title, format, size, created_at)"
            " VALUES ('n', 'book', 'n', 'txt', 1, 'x')"
        )
        assert conn.execute("SELECT id FROM items").fetchone()[0] == 1


def test_refused_on_non_empty_target(source, pg_dsn, gateway):
    with _pg(pg_dsn) as conn:
        conn.execute("INSERT INTO index_meta VALUES ('x', 'y')")
    embedder = gateway.embedder()
    with pytest.raises(s2p.TargetNotEmptyError, match="index_meta"):
        s2p.copy_library(source, pg_dsn, embedder=embedder)
    assert _library_rows(pg_dsn) == 1


def test_refused_when_capabilities_unreachable(source, pg_dsn, gateway):
    gateway.caps = None
    embedder = gateway.embedder()
    with pytest.raises(EmbeddingError, match="capabilities"):
        s2p.copy_library(source, pg_dsn, embedder=embedder)
    assert _library_rows(pg_dsn) == 0


def _poke(path, sql, params=()):
    conn = fx.connect(path)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    ("sql", "table", "column", "row", "reason"),
    [
        (
            "UPDATE feed SET data = '{\"x\": NaN}' WHERE cursor = 1",
            "feed",
            "data",
            "seq=1",
            "NaN",
        ),
        (
            "UPDATE eval_runs SET aggregates_json = '{\"mrr\": Infinity}'",
            "eval_runs",
            "aggregates_json",
            "id=1",
            "Infinity",
        ),
        (
            "UPDATE projects SET domains = '[\"a\\u0000b\"]'",
            "projects",
            "domains",
            "id=3",
            "\\u0000",
        ),
        (
            "UPDATE items SET title = 'a' || char(0) || 'b' WHERE id = 2",
            "items",
            "title",
            "id=2",
            "NUL",
        ),
        (
            "INSERT INTO search_fts (rowid, title, body) VALUES (9, 'orphan', 'no item')",
            "item_text",
            "item_id",
            "item_id=9",
            "orphan",
        ),
        (
            "INSERT INTO index_queue (item_id, enqueued_at) VALUES (4, 0)",
            "index_queue",
            "item_id",
            "item_id=4",
            "orphan",
        ),
        (
            "UPDATE embeddings SET vec = zeroblob(16) WHERE item_id = 2",
            "embeddings",
            "vec",
            "item_id=2,chunk_no=0",
            "4 dimensions",
        ),
        (
            "UPDATE repodoc_embeddings SET model = 'gemma2-embed'",
            "repodoc_embeddings",
            "model_id",
            "doc_id=1,chunk_no=0",
            "stale",
        ),
        (
            "UPDATE eval_runs SET running = 2",
            "eval_runs",
            "running",
            "id=1",
            "0/1",
        ),
    ],
)
def test_problem_rows_refuse_and_write_nothing(
    source, pg_dsn, gateway, sql, table, column, row, reason
):
    _poke(source, sql)
    report = s2p.copy_library(source, pg_dsn, embedder=gateway.embedder())
    assert report.status == "refused"
    assert not report.ok
    assert [(p.table, p.column, p.row) for p in report.problems] == [(table, column, row)]
    assert reason in report.problems[0].reason
    assert _library_rows(pg_dsn) == 0
    assert f"{table}.{column} [{row}]" in report.render()


def test_nan_vector_is_a_problem(source, pg_dsn, gateway):
    vals = array("f", [0.5] * 768)
    vals[7] = float("nan")
    _poke(source, "UPDATE embeddings SET vec = ? WHERE item_id = 2", (vals.tobytes(),))
    report = s2p.copy_library(source, pg_dsn, embedder=gateway.embedder())
    assert report.status == "refused"
    assert "NaN" in report.problems[0].reason


def test_invalid_utf8_text_is_a_problem(source, pg_dsn, gateway):
    _poke(source, "UPDATE items SET author = CAST(x'41ff42' AS TEXT) WHERE id = 1")
    report = s2p.copy_library(source, pg_dsn, embedder=gateway.embedder())
    assert [(p.table, p.column) for p in report.problems] == [("items", "author")]
    assert "UTF-8" in report.problems[0].reason


def test_drop_stale_vectors_skips_them_and_reports_it(source, pg_dsn, gateway):
    _poke(source, "UPDATE embeddings SET model = 'gemma2-embed' WHERE item_id = 1")
    report = s2p.copy_library(source, pg_dsn, embedder=gateway.embedder(), drop_stale_vectors=True)
    assert report.status == "copied", report.render()
    by = {t.table: t for t in report.tables}
    assert (by["embeddings"].source_count, by["embeddings"].skipped) == (3, 2)
    assert by["embeddings"].target_count == 1
    assert by["embeddings"].ok
    assert report.labels["embeddings"] == {"gemma2-embed": 2, fx.LABEL: 1}


def test_unknown_source_table_or_column_is_refused(source, pg_dsn, gateway):
    _poke(source, "CREATE TABLE extra (x)")
    embedder = gateway.embedder()
    with pytest.raises(s2p.SourceError, match="unknown extra"):
        s2p.copy_library(source, pg_dsn, embedder=embedder)
    _poke(source, "DROP TABLE extra")
    _poke(source, "ALTER TABLE items ADD COLUMN surprise TEXT")
    with pytest.raises(s2p.SourceError, match="surprise"):
        s2p.copy_library(source, pg_dsn, embedder=embedder)
    assert _library_rows(pg_dsn) == 0


def test_source_is_read_only(source, pg_dsn, gateway):
    before = source.read_bytes()
    s2p.copy_library(source, pg_dsn, embedder=gateway.embedder())
    assert source.read_bytes() == before
    assert not list(source.parent.glob("library.db-*"))


def test_verify_reports_a_difference(source, pg_dsn, gateway):
    assert s2p.copy_library(source, pg_dsn, embedder=gateway.embedder()).ok
    good = s2p.verify_library(source, pg_dsn, embedder=gateway.embedder())
    assert good.status == "verified"
    assert good.ok
    with _pg(pg_dsn) as conn:
        conn.execute("DELETE FROM eval_samples WHERE id = 2")
        conn.execute("UPDATE item_text SET body = 'changed' WHERE item_id = 2")
    bad = s2p.verify_library(source, pg_dsn, embedder=gateway.embedder())
    assert bad.status == "mismatch"
    assert not bad.ok
    by = {t.table: t for t in bad.tables}
    assert (by["eval_samples"].target_count, by["eval_samples"].target_max_id) == (1, 1)
    assert not by["eval_samples"].ok
    assert by["item_text"].digest_ok is False
    assert not by["item_text"].ok
    assert sum(not t.ok for t in bad.tables) == 2


def test_mismatch_during_copy_rolls_back(source, pg_dsn, gateway, monkeypatch):
    real = s2p._target_stats

    def off_by_one(conn, table):
        count, top = real(conn, table)
        return (count + 1, top) if table == "projects" else (count, top)

    monkeypatch.setattr(s2p, "_target_stats", off_by_one)
    report = s2p.copy_library(source, pg_dsn, embedder=gateway.embedder())
    assert report.status == "mismatch"
    assert _library_rows(pg_dsn) == 0


# ------------------------------------------------------------------------------ CLI


def _cli(gateway, source, *extra):
    return [
        "migrate",
        "sqlite-to-postgres",
        "--from",
        str(source),
        "--gateway-url",
        gateway.url,
        *extra,
    ]


def test_cli_copies_and_prints_report(source, pg_dsn, gateway, capsys):
    assert main(_cli(gateway, source)) == 0
    out = capsys.readouterr().out
    assert "COPIED" in out
    assert "item_text <- search_fts" in out
    assert "embeddings" in out
    assert "same" in out


def test_cli_json_report(source, pg_dsn, gateway, capsys):
    assert main(_cli(gateway, source, "--json")) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "copied"
    assert out["ok"] is True
    assert out["model_id"] == CURRENT_MODEL_ID
    tables = {t["table"]: t for t in out["tables"]}
    assert tables["feed"]["source_count"] == tables["feed"]["target_count"] == 4
    assert tables["items"]["target_max_id"] == 5


def test_cli_problem_exits_non_zero_with_report(source, pg_dsn, gateway, capsys):
    _poke(source, "UPDATE feed SET data = 'NaN' WHERE cursor = 4")
    assert main(_cli(gateway, source, "--json")) == 1
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["status"] == "refused"
    assert out["problems"][0]["table"] == "feed"
    assert "hint:" in captured.err or '"remediation"' in captured.err


def test_cli_verify_mismatch_exits_non_zero(source, pg_dsn, gateway, capsys):
    assert main(_cli(gateway, source)) == 0
    with _pg(pg_dsn) as conn:
        conn.execute("DELETE FROM api_keys WHERE id = 2")
    capsys.readouterr()
    assert main(_cli(gateway, source, "--verify-only")) == 1
    captured = capsys.readouterr()
    assert "MISMATCH" in captured.out
    assert "DIFF" in captured.out
    assert "hint:" in captured.err


def test_cli_non_empty_target(source, pg_dsn, gateway, capsys):
    with _pg(pg_dsn) as conn:
        conn.execute(
            "INSERT INTO items (sha256, kind, title, format, size, created_at)"
            " VALUES ('n', 'book', 'n', 'txt', 1, 'x')"
        )
    assert main(_cli(gateway, source)) == 1
    err = capsys.readouterr().err
    assert "already holds library rows" in err
    assert "hint:" in err


def test_cli_unreachable_gateway_is_env_error(source, pg_dsn, gateway, capsys):
    gateway.caps = None
    assert main(_cli(gateway, source)) == 2
    err = capsys.readouterr().err
    assert "capabilities" in err
    assert "hint:" in err


def test_cli_missing_source(tmp_path, gateway, capsys):
    assert main(_cli(gateway, tmp_path / "nope.db")) == 1
    assert "hint:" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("dsn", "password"),
    [
        ("postgresql://u@127.0.0.1:1/d", ENV_PW),
        (f"postgresql://u:{INLINE_PW}@127.0.0.1:1/d", INLINE_PW),
    ],
    ids=["env-password", "inline-password"],  # the passwords are random per run
)
def test_cli_explicit_to_takes_the_env_password(source, gateway, monkeypatch, dsn, password):
    from psycopg.conninfo import conninfo_to_dict

    seen = []

    def capture(_source, conninfo, **_kw):
        seen.append(conninfo)
        raise s2p.SourceError("stop here")

    monkeypatch.setenv("CULTURE_LIBRARY_PG_PASSWORD", ENV_PW)
    monkeypatch.setattr(s2p, "copy_library", capture)
    assert main(_cli(gateway, source, "--to", dsn)) == 1
    assert conninfo_to_dict(seen[0])["password"] == password
    assert conninfo_to_dict(seen[0])["dbname"] == "d"


def test_cli_overview_lists_the_verb(capsys):
    assert main(["migrate", "overview"]) == 0
    assert "sqlite-to-postgres" in capsys.readouterr().out


def test_hnsw_indexes_are_rebuilt_after_the_copy(source, pg_dsn, gateway):
    from culture_library.core.embedding_strategy import hnsw_index_name

    with _pg(pg_dsn) as conn:
        before = conn.execute("SELECT indexname, indexdef FROM pg_indexes ORDER BY 1").fetchall()
    s2p.copy_library(source, pg_dsn, embedder=gateway.embedder())
    with _pg(pg_dsn) as conn:
        after = conn.execute("SELECT indexname, indexdef FROM pg_indexes ORDER BY 1").fetchall()
        valid = conn.execute(
            "SELECT bool_and(indisvalid) FROM pg_index WHERE indrelid = 'embeddings'::regclass"
        ).fetchone()[0]
    assert after == before
    assert valid
    names = {n for n, _ in after}
    for table in s2p.VECTOR_TABLES:
        assert hnsw_index_name(table, CURRENT_MODEL_ID, CURRENT_STRATEGY) in names
