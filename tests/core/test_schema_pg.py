"""The initial Postgres schema against the live SQLite schema.

Plan library-on-postgres-and-pgvector, task t4.

``tests/migrate/fixtures/sqlite_schema_0.19.8.sql`` is the schema of live 0.19.8. The mapping
below says where every SQLite table and column went; the tests check the migrated database
(the shared per-worker template, ``pg_dsn``) against it: tables, columns, types, nullability,
identities, keys, foreign keys, the weighted tsvector columns with their GIN indexes, and the
partial HNSW index per (model id, strategy).
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import psycopg  # the dev group has it; the suite needs Postgres, so no skip
import pytest

from culture_library.core import embedding_strategy as es  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "migrate" / "fixtures" / "sqlite_schema_0.19.8.sql"

# --- the mapping ---------------------------------------------------------------------------------

# SQLite table -> Postgres table. None: not ported, with the reason beside it.
TABLES: dict[str, str | None] = {
    "api_keys": "api_keys",
    "embeddings": "embeddings",
    "eval_run_samples": "eval_run_samples",
    "eval_runs": "eval_runs",
    "eval_samples": "eval_samples",
    "eval_sets": "eval_sets",
    "feed": "feed",
    "index_meta": "index_meta",
    "index_queue": "index_queue",
    "item_sources": "item_sources",
    "item_uploaders": "item_uploaders",
    "items": "items",
    "mirror_outbox": "mirror_outbox",
    "projects": "projects",
    "repodoc_dirty": "repodoc_dirty",
    "repodoc_drafts": "repodoc_drafts",
    "repodoc_embeddings": "repodoc_embeddings",
    "repodoc_index_queue": "repodoc_index_queue",
    "repodoc_versions": "repodoc_versions",
    "repodocs": "repodocs",
    "spec_links": "spec_links",
    # 0.19.8's in-process vector cache read its changes from vector_log. Postgres answers vector
    # queries from pgvector and keeps no cache, so the log has nothing to feed (lapse l5).
    "vector_log": None,
    # FTS5: the virtual tables become plain text tables with a generated weighted tsvector
    # (FTS_REPLACED); their shadow tables are FTS5's own storage and have no counterpart.
    "search_fts": None,
    "repodoc_fts": None,
    **{
        f"{fts}_{part}": None
        for fts in ("search_fts", "repodoc_fts")
        for part in ("config", "content", "data", "docsize", "idx")
    },
}

# FTS5 virtual table -> (Postgres table, {FTS column -> Postgres column}); rowid is the key.
FTS_REPLACED = {
    "search_fts": ("item_text", {"rowid": "item_id", "title": "title", "body": "body"}),
    "repodoc_fts": (
        "repodoc_text",
        {
            "rowid": "doc_id",
            "title": "title",
            "body": "body",
            "label": "label",
            "visibility": "visibility",
        },
    ),
}

# Tables that exist only on Postgres (besides the FTS replacements).
PG_ONLY_TABLES = {"schema_version"}

RENAMED = {("feed", "cursor"): "seq"}  # cursor is a keyword; the API still says "cursor"
# The SQLite "model" label ("gemma2-embed+Retrieval-document") names a gateway role, not a
# model; model_id + strategy replace it (decision c42), filled by the t14 copy.
DROPPED = {("embeddings", "model"), ("repodoc_embeddings", "model")}
# vec_hash: an indexed md5 of the vector, for completing exact ties (migration 0003, d18).
ADDED = {
    "embeddings": {"model_id", "strategy", "vec_hash"},
    "repodoc_embeddings": {"model_id", "strategy", "vec_hash"},
    # simple_tsv: the unstemmed body, for a trailing stopword (migration 0004, d22).
    "item_text": {"tsv", "simple_tsv"},
    "repodoc_text": {"tsv", "simple_tsv"},
}

BOOLEANS = {
    ("api_keys", "private"),
    ("eval_runs", "running"),
    ("index_queue", "body_done"),
    ("repodoc_versions", "on_default_branch"),
}
JSONB = {
    ("feed", "data"),
    ("eval_run_samples", "retrieved_chunk_shas"),
    ("eval_run_samples", "scores_json"),
    ("eval_run_samples", "reference_chunk_shas"),
    ("eval_run_samples", "retrieved_item_ids"),
    ("eval_run_samples", "freshness"),
    ("eval_runs", "config_json"),
    ("eval_runs", "aggregates_json"),
    ("eval_runs", "partial_aggregates_json"),
    ("eval_runs", "suites_json"),
    ("eval_samples", "reference_chunk_shas"),
    ("eval_samples", "reference_contexts"),
    ("eval_samples", "source_urls"),
    ("eval_sets", "chunker_json"),
    ("eval_sets", "generator_json"),
    ("item_sources", "manifest_json"),
    ("projects", "domains"),
    ("projects", "connected"),
    ("spec_links", "code_paths_json"),
    ("spec_links", "trailer_commits_json"),
}
# TEXT kept on purpose though it looks structured: api_keys scopes/kinds are comma lists that
# keys.py splits, not JSON; index_meta.value is an opaque string.
VECTORS = {("embeddings", "vec"), ("repodoc_embeddings", "vec")}
# INTEGER PRIMARY KEY columns that are not AUTOINCREMENT but are still the table's own id.
EXTRA_IDENTITIES = {("projects", "id")}

INTEGER_TYPES = {"bigint", "integer"}


# --- reading the fixture -------------------------------------------------------------------------


def _fixture_blocks() -> dict[str, str]:
    """``-- table NAME`` block -> its CREATE statement."""
    text = FIXTURE.read_text(encoding="utf-8")
    blocks = re.split(r"^-- (table|index|trigger) (\S+)\n", text, flags=re.M)
    out = {}
    for i in range(1, len(blocks), 3):
        if blocks[i] == "table":
            out[blocks[i + 1]] = blocks[i + 2].strip()
    return out


def _sqlite_schema() -> sqlite3.Connection:
    """The fixture's ordinary tables and indexes in an in-memory SQLite (no FTS5 shadows)."""
    conn = sqlite3.connect(":memory:")
    text = FIXTURE.read_text(encoding="utf-8")
    for kind, name, body in re.findall(
        r"^-- (table|index) (\S+)\n(.*?)(?=^-- (?:table|index|trigger) |\Z)",
        text,
        flags=re.M | re.S,
    ):
        if kind == "table" and TABLES.get(name) is None:
            continue
        conn.execute(body.strip().rstrip(";"))
    return conn


def _sqlite_columns(conn, table):
    return [
        {"name": r[1], "type": r[2].upper(), "notnull": bool(r[3]) or r[5] > 0, "pk": r[5]}
        for r in conn.execute(f'PRAGMA table_info("{table}")')
    ]


@pytest.fixture(scope="module")
def sqlite_conn():
    conn = _sqlite_schema()
    yield conn
    conn.close()


@pytest.fixture
def pg(pg_dsn):
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        yield conn


def _pg_columns(pg, table) -> dict[str, dict]:
    rows = pg.execute(
        "SELECT column_name, data_type, udt_name, is_nullable, is_identity, identity_generation,"
        " is_generated, column_default FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    ).fetchall()
    return {
        r[0]: {
            "type": r[2] if r[1] == "USER-DEFINED" else r[1],
            "nullable": r[3] == "YES",
            "identity": r[5] if r[4] == "YES" else None,
            "generated": r[6] == "ALWAYS",
            "default": r[7],
        }
        for r in rows
    }


def _pg_tables(pg) -> set[str]:
    rows = pg.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
    return {r[0] for r in rows}


def _pg_name(table: str, column: str) -> str | None:
    if (table, column) in DROPPED:
        return None
    return RENAMED.get((table, column), column)


# --- tables and columns --------------------------------------------------------------------------


def test_fixture_tables_are_all_mapped():
    assert set(_fixture_blocks()) == set(TABLES)
    assert len(TABLES) == 34
    for fts in FTS_REPLACED:
        assert "VIRTUAL TABLE" in _fixture_blocks()[fts]
        assert "fts5" in _fixture_blocks()[fts]


def test_postgres_has_exactly_the_mapped_tables(pg):
    expected = {t for t in TABLES.values() if t} | PG_ONLY_TABLES
    expected |= {pg_table for pg_table, _ in FTS_REPLACED.values()}
    assert _pg_tables(pg) == expected


@pytest.mark.parametrize("table", sorted(t for t, p in TABLES.items() if p))
def test_columns_match_the_sqlite_schema(table, sqlite_conn, pg):
    pg_cols = _pg_columns(pg, TABLES[table])
    lite = _sqlite_columns(sqlite_conn, table)
    mapped = {_pg_name(table, c["name"]) for c in lite} - {None}
    assert set(pg_cols) == mapped | ADDED.get(table, set())

    autoinc = "AUTOINCREMENT" in _fixture_blocks()[table]
    for col in lite:
        name = _pg_name(table, col["name"])
        if name is None:
            continue
        got = pg_cols[name]
        key = (table, col["name"])
        if key in BOOLEANS:
            want = {"boolean"}
        elif key in JSONB:
            want = {"jsonb"}
        elif key in VECTORS:
            want = {"vector"}
        elif col["type"] == "INTEGER":
            want = INTEGER_TYPES
        elif col["type"] == "REAL":
            want = {"double precision"}
        elif col["type"] in ("TEXT", ""):
            want = {"text"}  # timestamps included: same ISO strings, byte for byte
        else:  # pragma: no cover - a new SQLite type needs a mapping decision
            pytest.fail(f"{table}.{col['name']}: unmapped SQLite type {col['type']!r}")
        assert got["type"] in want, f"{table}.{name}: {got['type']} not in {want}"
        assert got["nullable"] == (not col["notnull"]), f"{table}.{name} nullability"
        own_id = (
            col["pk"] == 1 and col["type"] == "INTEGER" and (autoinc or key in EXTRA_IDENTITIES)
        )
        assert got["identity"] == ("BY DEFAULT" if own_id else None), f"{table}.{name} identity"
        if key in BOOLEANS:
            assert got["default"] == "false"


@pytest.mark.parametrize("fts", sorted(FTS_REPLACED))
def test_fts_columns_have_a_home(fts, pg):
    table, columns = FTS_REPLACED[fts]
    block = _fixture_blocks()[fts]
    declared = re.search(r"fts5\((.*)\)", block, flags=re.S).group(1)
    fts_cols = [c.split()[0] for c in declared.split(",") if "=" not in c]
    assert set(columns) == {"rowid", *fts_cols}
    pg_cols = _pg_columns(pg, table)
    assert set(pg_cols) == set(columns.values()) | ADDED[table]
    assert pg_cols["tsv"]["type"] == "tsvector"
    assert pg_cols["tsv"]["generated"]
    for name in columns.values():
        assert not pg_cols[name]["nullable"], f"{table}.{name}"


def test_primary_keys_match(sqlite_conn, pg):
    for table, pg_table in TABLES.items():
        if pg_table is None:
            continue
        lite = sorted(
            (c["pk"], _pg_name(table, c["name"]))
            for c in _sqlite_columns(sqlite_conn, table)
            if c["pk"]
        )
        want = [name for _, name in lite]
        if table in ADDED and "embeddings" in table:
            # vectors of two (model, strategy) pairs live side by side for the same chunk
            want = [want[0], "model_id", "strategy", want[1]]
        rows = pg.execute(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid"
            " AND a.attnum = ANY(i.indkey) WHERE i.indrelid = %s::regclass AND i.indisprimary"
            " ORDER BY array_position(i.indkey, a.attnum)",
            (pg_table,),
        ).fetchall()
        assert [r[0] for r in rows] == want, pg_table


def test_unique_constraints_match(sqlite_conn, pg):
    for table, pg_table in TABLES.items():
        if pg_table is None:
            continue
        lite = set()
        for idx in sqlite_conn.execute(f'PRAGMA index_list("{table}")'):
            if idx[3] == "u":
                cols = [r[2] for r in sqlite_conn.execute(f'PRAGMA index_info("{idx[1]}")')]
                lite.add(tuple(_pg_name(table, c) for c in cols))
        rows = pg.execute(
            "SELECT array_agg(a.attname ORDER BY array_position(c.conkey, a.attnum))"
            " FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid"
            " AND a.attnum = ANY(c.conkey) WHERE c.conrelid = %s::regclass AND c.contype = 'u'"
            " GROUP BY c.oid",
            (pg_table,),
        ).fetchall()
        assert {tuple(r[0]) for r in rows} == lite, pg_table


def test_foreign_keys_match(sqlite_conn, pg):
    actions = {"CASCADE": "c", "NO ACTION": "a"}
    for table, pg_table in TABLES.items():
        if pg_table is None:
            continue
        lite = {
            (_pg_name(table, r[3]), r[2], r[4] or "id", actions[r[6]])
            for r in sqlite_conn.execute(f'PRAGMA foreign_key_list("{table}")')
        }
        rows = pg.execute(
            "SELECT a.attname, c.confrelid::regclass::text, fa.attname, c.confdeltype"
            " FROM pg_constraint c"
            " JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]"
            " JOIN pg_attribute fa ON fa.attrelid = c.confrelid AND fa.attnum = c.confkey[1]"
            " WHERE c.conrelid = %s::regclass AND c.contype = 'f'",
            (pg_table,),
        ).fetchall()
        assert {tuple(r) for r in rows} == lite, pg_table


def test_text_tables_cascade_from_their_owner(pg):
    for table, col, ref in (
        ("item_text", "item_id", "items"),
        ("repodoc_text", "doc_id", "repodocs"),
    ):
        row = pg.execute(
            "SELECT c.confrelid::regclass::text, c.confdeltype FROM pg_constraint c"
            " WHERE c.conrelid = %s::regclass AND c.contype = 'f'",
            (table,),
        ).fetchone()
        assert row == (ref, "c"), table


def test_sqlite_indexes_exist_under_the_same_names(sqlite_conn, pg):
    names = {
        r[0]
        for r in sqlite_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
        )
    }
    assert len(names) == 7
    rows = pg.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'").fetchall()
    assert names <= {r[0] for r in rows}


def test_no_identifier_needs_quoting(pg):
    rows = pg.execute(
        "SELECT table_name, column_name, quote_ident(column_name) FROM information_schema.columns"
        " WHERE table_schema = 'public'"
    ).fetchall()
    assert [(t, c) for t, c, q in rows if q != c] == []
    tables = pg.execute(
        "SELECT tablename, quote_ident(tablename) FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    assert [t for t, q in tables if q != t] == []


# --- behaviour the types promise -----------------------------------------------------------------


def _item(pg, title="Cats") -> int:
    return pg.execute(
        "INSERT INTO items (sha256, kind, title, format, size, created_at)"
        " VALUES (md5(random()::text), 'book', %s, 'epub', 1, '2026-10-31T00:00:00+00:00')"
        " RETURNING id",
        (title,),
    ).fetchone()[0]


def _repodoc(pg) -> int:
    return pg.execute(
        "INSERT INTO repodocs (repo, path, kind, created_at, updated_at)"
        " VALUES ('agentculture/x', 'docs/specs/a.md', 'spec', 't', 't') RETURNING id"
    ).fetchone()[0]


def test_feed_seq_is_returned_as_cursor_and_explicit_ids_are_accepted(pg):
    pg.execute(
        "INSERT INTO feed (seq, type, item_id, sha256, kind, at, data)"
        " VALUES (4041, 'added', 1, 's', 'book', '2026-10-31T00:00:00+00:00', %s)",
        ('{"b": 1, "a": [1.0, "x"]}',),
    )
    row = pg.execute("SELECT seq AS cursor, subject, visibility, at, data FROM feed").fetchone()
    assert row == (4041, "item", "private", "2026-10-31T00:00:00+00:00", {"a": [1.0, "x"], "b": 1})
    # the t14 copy inserts explicit ids, then moves the identity past them
    pg.execute("SELECT setval(pg_get_serial_sequence('feed', 'seq'), 4041)")
    nxt = pg.execute(
        "INSERT INTO feed (type, item_id, sha256, kind, at) VALUES ('added', 1, 's', 'book', 't')"
        " RETURNING seq"
    ).fetchone()[0]
    assert nxt == 4042


def test_check_constraints_survive(pg):
    item = _item(pg)
    bad = [
        "INSERT INTO items (sha256, kind, title, format, size, created_at)"
        " VALUES ('x', 'movie', 't', 'f', 1, 't')",
        "INSERT INTO mirror_outbox (key, sha256, state, enqueued_at)"
        " VALUES ('k', 's', 'gone', 't')",
        f"INSERT INTO index_queue (item_id, state, enqueued_at) VALUES ({item}, 'gone', 0)",
        "INSERT INTO repodocs (repo, path, kind, visibility, created_at, updated_at)"
        " VALUES ('r', 'p', 'spec', 'secret', 't', 't')",
    ]
    for sql in bad:
        with pytest.raises(psycopg.errors.CheckViolation):
            pg.execute(sql)
    set_id = pg.execute(
        "INSERT INTO eval_sets (item_id, item_sha256, chunker_json, adapter, adapter_version,"
        " generator_json, created_at) VALUES (%s, 's', '{}', 'ragas', '0.4.3', '{}', 't')"
        " RETURNING id",
        (item,),
    ).fetchone()[0]
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            "INSERT INTO eval_runs (set_id, status, config_json, aggregates_json, created_at)"
            " VALUES (%s, 'partial', '{}', '{}', 't')",
            (set_id,),
        )


# --- full text -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table,key,owner",
    [("item_text", "item_id", _item), ("repodoc_text", "doc_id", _repodoc)],
)
def test_tsvector_is_weighted_title_a_body_b_in_english(table, key, owner, pg):
    oid = owner(pg)
    extra = ", label, visibility" if table == "repodoc_text" else ""
    values = ", 'A label', 'public'" if table == "repodoc_text" else ""
    pg.execute(
        f"INSERT INTO {table} ({key}, title, body{extra})"
        f" VALUES (%s, 'Cats Running', 'the dogs jumped'{values})",
        (oid,),
    )
    tsv = pg.execute(f"SELECT tsv::text FROM {table} WHERE {key} = %s", (oid,)).fetchone()[0]
    assert tsv == "'cat':1A 'dog':4B 'jump':5B 'run':2A"
    # a body rewrite recomputes the vector
    pg.execute(f"UPDATE {table} SET body = 'birds' WHERE {key} = %s", (oid,))
    tsv = pg.execute(f"SELECT tsv::text FROM {table} WHERE {key} = %s", (oid,)).fetchone()[0]
    assert tsv == "'bird':3B 'cat':1A 'run':2A"


@pytest.mark.parametrize("table", ["item_text", "repodoc_text"])
def test_tsvector_has_a_gin_index_the_planner_uses(table, pg):
    row = pg.execute(
        "SELECT i.indexrelid::regclass::text, am.amname, pg_get_indexdef(i.indexrelid)"
        " FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_am am ON am.oid = c.relam"
        " WHERE i.indrelid = %s::regclass AND am.amname = 'gin'",
        (table,),
    ).fetchone()
    assert row is not None, row
    assert row[2].endswith("USING gin (tsv)"), row
    with pg.transaction():
        pg.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(
            r[0]
            for r in pg.execute(
                f"EXPLAIN SELECT 1 FROM {table} WHERE tsv @@ to_tsquery('english', 'cat')"
            )
        )
    assert row[0] in plan, plan


# --- vectors -------------------------------------------------------------------------------------


def _vec(n: int, hot: int) -> str:
    return "[" + ",".join("1" if i == hot else "0" for i in range(n)) + "]"


def test_vector_extension_is_created(pg):
    assert pg.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'").fetchone()


def test_current_strategy_key_is_stable():
    s = es.CURRENT_STRATEGY
    assert (s.document_prompt, s.query_prompt) == ("Retrieval-document", "Retrieval-query")
    assert (s.chunker, s.max_chars, s.dimension, s.normalisation) == (
        "paragraph-pack",
        1000,
        768,
        "l2",
    )
    assert s.key() == (
        '{"chunker":"paragraph-pack","dimension":768,"document_prompt":"Retrieval-document",'
        '"max_chars":1000,"normalisation":"l2","query_prompt":"Retrieval-query"}'
    )
    assert es.Strategy.from_key(s.key()) == s
    assert es.CURRENT_MODEL_ID == "google/embeddinggemma-2"
    with pytest.raises(ValueError):
        es.Strategy.from_key('{"chunker":"paragraph"}')


@pytest.mark.parametrize("table", ["embeddings", "repodoc_embeddings"])
def test_hnsw_index_for_the_current_model_and_strategy(table, pg):
    name = es.hnsw_index_name(table, es.CURRENT_MODEL_ID, es.CURRENT_STRATEGY)
    assert len(name) <= 63
    row = pg.execute(
        "SELECT pg_get_indexdef(i.indexrelid), pg_get_expr(i.indpred, i.indrelid)"
        " FROM pg_index i WHERE i.indexrelid = to_regclass(%s)",
        (name,),
    ).fetchone()
    assert row is not None, f"no index {name}"
    indexdef, predicate = row
    assert "USING hnsw (((vec)::vector(768)) vector_cosine_ops)" in indexdef
    assert f"model_id = '{es.CURRENT_MODEL_ID}'::text" in predicate
    assert f"strategy = '{es.CURRENT_STRATEGY.key()}'::text" in predicate
    # the helper t13 uses for a new (model, strategy) builds exactly this index
    with pg.transaction():
        pg.execute(f"DROP INDEX {name}")
        pg.execute(es.hnsw_index_sql(table, es.CURRENT_MODEL_ID, es.CURRENT_STRATEGY))
        rebuilt = pg.execute("SELECT pg_get_indexdef(to_regclass(%s))", (name,)).fetchone()[0]
        assert rebuilt == indexdef
        raise psycopg.Rollback()  # leave the template's index as the migration made it
    assert pg.execute("SELECT to_regclass(%s)", (name,)).fetchone()[0] is not None


@pytest.mark.parametrize(
    "table,key,owner",
    [("embeddings", "item_id", _item), ("repodoc_embeddings", "doc_id", _repodoc)],
)
def test_vectors_of_two_strategies_side_by_side_and_the_index_serves_only_its_own(
    table, key, owner, pg
):
    oid = owner(pg)
    current = (es.CURRENT_MODEL_ID, es.CURRENT_STRATEGY.key())
    other_strategy = es.Strategy("q", "d", "paragraph-pack", 2000, 4, "l2")
    other = ("example/other-model", other_strategy.key())
    for n in range(3):
        pg.execute(
            f"INSERT INTO {table} ({key}, chunk_no, text, vec, model_id, strategy)"
            " VALUES (%s, %s, 'chunk', %s::vector, %s, %s)",
            (oid, n, _vec(768, n), *current),
        )
        pg.execute(
            f"INSERT INTO {table} ({key}, chunk_no, text, vec, model_id, strategy)"
            " VALUES (%s, %s, 'chunk', %s::vector, %s, %s)",
            (oid, n, _vec(4, n), *other),
        )
    # a vector whose length disagrees with its strategy's dimension is refused
    params = (oid, _vec(4, 0), *current)
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            f"INSERT INTO {table} ({key}, chunk_no, text, vec, model_id, strategy)"
            " VALUES (%s, 9, 'chunk', %s::vector, %s, %s)",
            params,
        )
    name = es.hnsw_index_name(table, es.CURRENT_MODEL_ID, es.CURRENT_STRATEGY)
    query = (
        f"SELECT chunk_no FROM {table} WHERE model_id = %s AND strategy = %s"
        " ORDER BY vec::vector(768) <=> %s::vector(768) LIMIT 2"
    )
    with pg.transaction():
        pg.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(r[0] for r in pg.execute("EXPLAIN " + query, (*current, _vec(768, 1))))
        assert name in plan, plan
        got = [r[0] for r in pg.execute(query, (*current, _vec(768, 1)))]
    assert got[0] == 1
    rows = pg.execute(
        f"SELECT count(*) FROM {table} WHERE model_id = %s AND strategy = %s", other
    ).fetchone()
    assert rows == (3,)


def test_hnsw_index_sql_quotes_literals():
    s = es.Strategy("it's", "q", "paragraph-pack", 1000, 768, "l2")
    sql = es.hnsw_index_sql("embeddings", "o'model", s)
    assert "'o''model'" in sql
    assert "it''s" in sql
    with pytest.raises(ValueError):
        es.hnsw_index_sql("embeddings; DROP TABLE items", "m", s)


def test_text_defaults_come_from_one_function_each(pg):
    """Visibility defaults to 'private' and job state to 'pending' on every table that has
    them; both come from a single SQL function each (no duplicated literals)."""
    rows = pg.execute(
        "SELECT table_name, column_name, column_default FROM information_schema.columns"
        " WHERE table_schema = 'public' AND column_name IN"
        " ('visibility', 'shown_visibility', 'state') AND column_default IS NOT NULL"
    ).fetchall()
    assert rows
    for table, column, default in rows:
        want = "library_default_state()" if column == "state" else "library_default_visibility()"
        assert want in default, (table, column, default)
    assert pg.execute("SELECT public.library_default_visibility()").fetchone()[0] == "private"
    assert pg.execute("SELECT public.library_default_state()").fetchone()[0] == "pending"
