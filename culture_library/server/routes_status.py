"""``GET /api/status``: live counts, index health, last pushes and the feed cursor (read scope).

Everything is computed from the database on each call; nothing is cached.

Visibility choice: a caller without ``Principal.private`` sees public numbers only (private
kinds, counts and push times are omitted, not zeroed) and ``private_included`` is ``false``.
The feed cursor and the index queue figures are global operational numbers: they say how far
the log/indexer have got, not what the private items are. Archived items are not counted.

"Last push per source": items carry no source column, so this reports what the data holds
instead of inventing one: the newest ``created_at`` per item kind, the newest ``pushed_at``
per repo-doc kind, and the newest feed event time per event type. ``source_note`` says so.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from culture_library.core import Store
from culture_library.core import indexer as ix
from culture_library.core import search as sr
from culture_library.server.auth import Principal, check, current_principal, get_store

router = APIRouter()

SOURCE_NOTE = (
    "items have no source field; last push is the newest created_at per item kind, the newest "
    "pushed_at per repo-doc kind, and the newest event time per feed event type"
)


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


def _retrieval_config(state) -> dict:
    """The configured retrieval setup, from the embedders and reranker on ``app.state``.

    ``embed_model`` is the document embedder's model; ``embed_prompts`` is ``None`` without an
    embedder or when no prompt is sent. Nothing here calls the gateway.
    """
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
        "chunker_max_chars": sr.CHUNK_MAX_CHARS,
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
        row[0] for row in conn.execute(f"SELECT DISTINCT e.model {frm} ORDER BY 1")
    ]  # nosec B608
    return count, models


def collect(store: Store, private: bool, state=None) -> dict:
    """Build the status document for a caller with (or without) private visibility.

    ``state`` is ``app.state`` (for the ``retrieval`` block); without it that block reports
    no embedder or reranker.
    """
    with store.reader() as conn:
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
        cursor = conn.execute("SELECT COALESCE(MAX(cursor), 0) FROM feed").fetchone()[0]

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
    }


@router.get("/api/status", tags=["status"])
def status(request: Request, principal: Annotated[Principal, Depends(current_principal)]) -> dict:
    check(principal, "read")
    return collect(get_store(request), principal.private, request.app.state)
