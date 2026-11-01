"""Generated, indexed columns added after 0001: vec_hash (migration 0003) and simple_tsv (0004).

vec_hash is an md5 of each vector, so search can find the chunks identical to a tied one
(core/search.py, ``_ann``); simple_tsv is the unstemmed body, for a trailing stopword.
"""

import psycopg
import pytest


@pytest.mark.parametrize("table", ["embeddings", "repodoc_embeddings"])
def test_vec_hash_is_generated_from_the_vector_and_indexed(pg_dsn, table):
    with psycopg.connect(pg_dsn) as conn:
        col = conn.execute(
            "SELECT data_type, is_generated, generation_expression FROM information_schema.columns"
            " WHERE table_name = %s AND column_name = 'vec_hash'",
            (table,),
        ).fetchone()
        assert col[0] == "uuid"
        assert col[1] == "ALWAYS"
        assert "md5" in col[2]
        defs = [r[0] for r in conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = %s", (table,)
        )]  # fmt: skip
        assert any("(model_id, strategy, vec_hash)" in d for d in defs), defs
        same = conn.execute(
            "SELECT md5('[1,2.5,-0]'::vector::text)::uuid = md5('[1,2.5,-0]'::vector::text)::uuid,"
            " md5('[1,2.5]'::vector::text)::uuid <> md5('[1,2.5000002]'::vector::text)::uuid"
        ).fetchone()
        assert same == (True, True)


@pytest.mark.parametrize("table", ["item_text", "repodoc_text"])
def test_simple_tsv_is_the_unstemmed_body_with_a_gin_index(pg_dsn, table):
    """Migration 0004: the body's 'simple' tsvector keeps stopwords the english tsv drops."""
    with psycopg.connect(pg_dsn) as conn:
        col = conn.execute(
            "SELECT is_generated, generation_expression FROM information_schema.columns"
            " WHERE table_name = %s AND column_name = 'simple_tsv'",
            (table,),
        ).fetchone()
        assert col[0] == "ALWAYS"
        assert "simple" in col[1]
        defs = [r[0] for r in conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = %s", (table,)
        )]  # fmt: skip
        assert any("gin (simple_tsv)" in d for d in defs), defs
        kept = conn.execute("SELECT to_tsvector('simple', 'ship with it')::text").fetchone()[0]
        assert "'with'" in kept
