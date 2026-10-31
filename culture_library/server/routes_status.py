"""``GET /api/status``: live counts, index health, last pushes and the feed cursor (read scope).

Everything is computed from the database on each call; nothing is cached.

Visibility choice: a caller without ``Principal.private`` sees public numbers only (private
kinds, counts and push times are omitted, not zeroed) and ``private_included`` is ``false``.
The feed cursor, the index queue and the mirror backlog figures are global operational
numbers: they say how far the log/indexer/mirror have got, not what the private items are.
Archived items are not counted.

"Last push per source": items carry no source column, so this reports what the data holds
instead of inventing one: the newest ``created_at`` per item kind, the newest ``pushed_at``
per repo-doc kind, and the newest feed event time per event type. ``source_note`` says so.

``database`` is :func:`database_block`: ``{reachable, schema_version, expected_schema_version,
schema_current, vector_indexes}``. Status still answers (200) when Postgres is stopped: the
block then says ``reachable: false`` and the database-backed keys are ``null``, while
``/api/health`` fails. A caller whose key cannot be checked for the same reason gets only the
``database`` block.

``mirror`` is :func:`culture_library.server.routes_mirror.status_block`: ``{enabled, pending,
oldest_pending_age_s, last_error}`` for the repo-document outbox.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Request

from culture_library.core import Store, migrations
from culture_library.core.store import DatabaseError
from culture_library.server.auth import Principal, check, current_principal, get_store

router = APIRouter()

SOURCE_NOTE = (
    "items have no source field; last push is the newest created_at per item kind, the newest "
    "pushed_at per repo-doc kind, and the newest event time per feed event type"
)


VECTOR_TABLES = ("embeddings", "repodoc_embeddings")
_PRED = re.compile(r"model_id = '((?:[^']|'')*)'(?:::text)?\)? AND \(?strategy = '((?:[^']|'')*)'")

_INDEX_SQL = """
SELECT c.relname, i.indisvalid AND i.indisready, pg_get_expr(i.indpred, i.indrelid)
FROM pg_index i
JOIN pg_class c ON c.oid = i.indexrelid
JOIN pg_class t ON t.oid = i.indrelid
JOIN pg_am am ON am.oid = c.relam
WHERE t.relname = %s AND t.relnamespace = current_schema()::regnamespace
  AND am.amname = 'hnsw' ORDER BY c.relname
"""


def _vector_indexes(conn, table: str) -> dict:
    """The HNSW indexes on ``table`` (name, model id, strategy key, valid) and the stored
    (model id, strategy) pairs that no index serves yet."""
    indexes, covered = [], set()
    for name, valid, pred in conn.execute(_INDEX_SQL, (table,)).fetchall():
        found = _PRED.search(pred or "")
        model, strategy = (v.replace("''", "'") for v in found.groups()) if found else (None, None)
        covered.add((model, strategy))
        indexes.append(
            {"name": name, "model_id": model, "strategy": strategy, "valid": bool(valid)}
        )
    pairs = conn.execute(
        f"SELECT DISTINCT model_id, strategy FROM {table} ORDER BY 1, 2"  # nosec B608
    ).fetchall()
    unindexed = [{"model_id": m, "strategy": st} for m, st in pairs if (m, st) not in covered]
    return {"indexes": indexes, "unindexed": unindexed}


def database_block(store: Store) -> dict:
    """Reachability, schema version and vector index state; never raises for a stopped database.

    ``schema_version`` is the database's highest applied migration (``null`` when none or
    unreachable); ``schema_current`` is true when it equals this release's
    ``expected_schema_version``.
    """
    block = {
        "reachable": False,
        "schema_version": None,
        "expected_schema_version": migrations.CURRENT_VERSION,
        "schema_current": False,
        "vector_indexes": None,
    }
    try:
        with store.reader() as conn:
            applied = conn.execute("SELECT to_regclass('schema_version') IS NOT NULL").fetchone()[0]
            version = (
                conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
                if applied
                else None
            )
            block["reachable"] = True
            block["schema_version"] = version
            block["schema_current"] = version == migrations.CURRENT_VERSION
            block["vector_indexes"] = (
                {table: _vector_indexes(conn, table) for table in VECTOR_TABLES}
                if version is not None
                else None
            )
    except (DatabaseError, store._psycopg.Error):
        block["reachable"] = False
    return block


def _vis_clause(column: str, private: bool) -> str:
    return "1=1" if private else f"{column} = 'public'"


def _tally(rows) -> tuple[int, dict, dict, dict]:
    total, by_kind, by_vis, by_kv = 0, {}, {}, {}
    for kind, vis, n in rows:
        total += n
        by_kind[kind] = by_kind.get(kind, 0) + n
        by_vis[vis] = by_vis.get(vis, 0) + n
        by_kv.setdefault(kind, {})[vis] = n
    return total, by_kind, by_vis, by_kv


def _retrieval_config(state, chunker: bool = True) -> dict:
    """The configured retrieval setup, from the embedders and reranker on ``app.state``.

    ``embed_model`` is the document embedder's model; ``embed_prompts`` is ``None`` without an
    embedder or when no prompt is sent. Nothing here calls the gateway.
    """
    if chunker:
        from culture_library.core import search as sr

        max_chars = sr.CHUNK_MAX_CHARS
    else:  # the database is down: report only what the process itself knows
        max_chars = None

    doc = getattr(state, "doc_embedder", None)
    query = getattr(state, "query_embedder", None)
    reranker = getattr(state, "reranker", None)
    doc_prompt = getattr(doc, "prompt_name", None)
    query_prompt = getattr(query, "prompt_name", None)
    prompts = None
    if (doc is not None or query is not None) and (doc_prompt or query_prompt):
        prompts = {"document": doc_prompt, "query": query_prompt}
    return {
        "embed_model": getattr(doc, "model", None),
        "embed_prompts": prompts,
        "chunker_max_chars": max_chars,
        "reranker_model": getattr(reranker, "model", None),
    }


def _stored_embeddings(conn, private: bool) -> tuple[int, list[str]]:
    """Chunk count and distinct embedding models of live items the caller may see."""
    vis = _vis_clause("i.visibility", private)
    frm = (
        "FROM embeddings e JOIN items i ON i.id = e.item_id "
        f"WHERE i.archived_at IS NULL AND {vis}"
    )
    count = conn.execute(f"SELECT COUNT(*) {frm}").fetchone()[0]  # nosec B608
    models = [
        row[0] for row in conn.execute(f"SELECT DISTINCT e.model_id {frm} ORDER BY 1")
    ]  # nosec B608
    return count, models


def collect(store: Store, private: bool, state=None) -> dict:
    """Build the status document for a caller with (or without) private visibility.

    ``state`` is ``app.state`` (for the ``retrieval`` block); without it that block reports
    no embedder or reranker.
    """
    with store.reader() as conn:
        from culture_library.core import indexer as ix
        from culture_library.server import routes_mirror

        chunk_count, embed_models = _stored_embeddings(conn, private)
        vis = _vis_clause("visibility", private)
        item_rows = conn.execute(
            "SELECT kind, visibility, COUNT(*) FROM items WHERE archived_at IS NULL "  # nosec B608
            f"AND {vis} GROUP BY 1, 2"
        ).fetchall()
        item_push = conn.execute(
            "SELECT kind, MAX(created_at) FROM items WHERE archived_at IS NULL "  # nosec B608
            f"AND {vis} GROUP BY 1"
        ).fetchall()
        doc_rows = conn.execute(
            f"SELECT kind, visibility, COUNT(*) FROM repodocs WHERE {vis} GROUP BY 1, 2"  # nosec
        ).fetchall()
        n_versions = conn.execute(
            f"SELECT COUNT(*) FROM repodoc_versions WHERE {vis}"  # nosec B608
        ).fetchone()[0]
        doc_push = conn.execute(
            "SELECT d.kind, MAX(v.pushed_at) FROM repodoc_versions v "  # nosec B608
            "JOIN repodocs d ON d.id = v.doc_id "
            f"WHERE {_vis_clause('v.visibility', private)} GROUP BY 1"
        ).fetchall()
        feed_push = conn.execute(
            f"SELECT type, MAX(at) FROM feed WHERE {vis} GROUP BY 1"  # nosec B608
        ).fetchall()
        cursor = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM feed").fetchone()[0]

    total, by_kind, by_vis, by_kv = _tally(item_rows)
    d_total, d_kind, d_vis, d_kv = _tally(doc_rows)
    return {
        "private_included": private,
        "counts": {
            "items": {
                "total": total,
                "by_kind": by_kind,
                "by_visibility": by_vis,
                "by_kind_visibility": by_kv,
            },
            "repodocs": {
                "total": d_total,
                "by_kind": d_kind,
                "by_visibility": d_vis,
                "by_kind_visibility": d_kv,
                "versions": n_versions,
            },
        },
        "index": ix.status(store),
        "last_push": {
            "items": {"by_kind": dict(item_push)},
            "repodocs": {"by_kind": dict(doc_push)},
            "feed": dict(feed_push),
            "source_note": SOURCE_NOTE,
        },
        "feed": {"cursor": cursor},
        "retrieval": {
            **_retrieval_config(state),
            "chunk_count": chunk_count,
            "embed_models_stored": embed_models,
        },
        "mirror": routes_mirror.status_block(store, state, private),
        "database": database_block(store),
    }


def _down() -> dict:
    return {
        "reachable": False,
        "schema_version": None,
        "expected_schema_version": migrations.CURRENT_VERSION,
        "schema_current": False,
        "vector_indexes": None,
    }


def _degraded(database: dict, state) -> dict:
    """The answer while the database is down: only what needs no query."""
    return {
        "private_included": None,
        "counts": None,
        "index": None,
        "last_push": None,
        "feed": None,
        "retrieval": {
            **_retrieval_config(state, chunker=False),
            "chunk_count": None,
            "embed_models_stored": None,
        },
        "mirror": None,
        "database": database,
    }


def _caller(request: Request) -> Principal:
    """``current_principal``, honouring ``dependency_overrides`` (it is called, not injected, so
    a stopped database can be answered instead of failing the request)."""
    override = request.app.dependency_overrides.get(current_principal)
    if override is not None:
        return override()
    return current_principal(request)


@router.get("/api/status", tags=["status"])
def status(request: Request) -> dict:
    try:
        principal = _caller(request)
        store = get_store(request)
    except DatabaseError:
        return _degraded(_down(), request.app.state)
    check(principal, "read")
    try:
        return collect(store, principal.private, request.app.state)
    except DatabaseError:
        return _degraded(database_block(store), request.app.state)
