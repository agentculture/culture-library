"""Migration 0003: every stored vector carries ``vec_hash``, an indexed md5 of its value.

Search uses it to find the chunks whose vector is identical to a tied one (core/search.py,
``_ann``); a hash is exact where comparing chunk text would only be a proxy.
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
        assert col[0] == "uuid" and col[1] == "ALWAYS"
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
