"""Retrieval for RAG and eval: chunk-level passages with optional rerank (read scope).

``POST /api/rag/retrieve`` with JSON ``{query, k (1-50, default 5), mode semantic|hybrid
(default hybrid), kind?, item_ids?, rerank?, candidates (k..200, default 50)}`` answers
``{query, mode, reranked, passages: [{item_id, title, kind, chunk_no, chunk_sha, text, score,
rerank_score, source, corpus}]}``. ``corpora`` (``["item"]`` by default, or ``["item",
"repodoc"]``) picks the corpora; repodoc passages have ``item_id`` null and add ``doc_id``,
``repo`` and ``path``. ``source`` stays semantic|hybrid.

* The query is embedded with ``app.state.query_embedder`` (set by the ``routes_search``
  lifespan or injected; without a lifespan :meth:`GatewayEmbedder.from_env` ``("query")`` is
  used and cached). ``app.state.embedder`` is ``/api/search``'s override and is *not* read
  here. No query embedder, or a failing one, answers 503 ``rag_unavailable``.
* Rerank: ``rerank`` defaults to true iff ``app.state.reranker`` is configured. When it runs,
  the top ``candidates`` passages are sent to the reranker and reordered by its score
  (passages it did not score keep their order after the scored ones, ``rerank_score``
  ``null``); the top ``k`` are returned with ``reranked: true``. With no reranker, or any
  reranker failure, the original order truncated to ``k`` is returned with
  ``reranked: false`` (still 200).
* Visibility is exactly ``/api/search``'s: private items only for ``Principal.private``
  callers and the key that uploaded them; archived items never appear.

``GET /api/rag/items/{id}/chunks`` answers ``{item_id, item_sha256, index_state
(pending|done|failed|none), embed_model (or null), chunks: [{chunk_no, chunk_sha, text,
is_header}]}`` for sample generators; an invisible, archived or missing item is 404
``not_found``. Chunk 0 is the indexer's title header (``is_header: true``).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from culture_library.core import KINDS, library
from culture_library.core import search as sr
from culture_library.core.errors import NotFoundError
from culture_library.gateway import GatewayEmbedder, GatewayReranker
from culture_library.server.auth import Principal, check, current_principal, get_store

router = APIRouter()


class RetrieveRequest(BaseModel):
    query: str = Field(..., min_length=1)
    k: int = Field(5, ge=1, le=50)
    mode: Literal["semantic", "hybrid"] = "hybrid"
    kind: Literal[KINDS] | None = None  # type: ignore[valid-type]
    item_ids: list[int] | None = None
    rerank: bool | None = None
    candidates: int = Field(50, ge=1, le=200)
    corpora: list[Literal["item", "repodoc"]] = Field(
        default_factory=lambda: ["item"], min_length=1
    )

    @model_validator(mode="after")
    def _candidates_cover_k(self) -> RetrieveRequest:
        if self.candidates < self.k:
            raise ValueError("candidates must be at least k")
        return self


def _vis(principal: Principal) -> sr.Vis:
    """The same visibility inputs ``/api/search`` builds from the caller."""
    return sr.Vis(include_private=principal.private, owner=principal.key_id)


def _query_embedder(request: Request) -> sr.Embedder | None:
    state = request.app.state
    if not hasattr(state, "query_embedder"):  # no lifespan ran (e.g. TestClient without with)
        state.query_embedder = GatewayEmbedder.from_env("query")
    return state.query_embedder


def _reranker(request: Request):
    state = request.app.state
    if not hasattr(state, "reranker"):
        state.reranker = GatewayReranker.from_env()
    return state.reranker


def _unavailable(reason: str) -> HTTPException:
    return HTTPException(status_code=503, detail={"detail": reason, "code": "rag_unavailable"})


def _rerank(reranker, query: str, cands: list[sr.Passage]) -> list[tuple[sr.Passage, float | None]]:
    """``cands`` reordered by the reranker's score; raises whatever the reranker raises.

    Index validity is the reranker's contract (``GatewayReranker`` raises ``RerankError``).
    """
    scored = reranker(query, [p.text for p in cands])
    best: dict[int, float] = {}
    for index, score in scored:
        i, s = int(index), float(score)
        if i not in best or s > best[i]:
            best[i] = s
    order = sorted(best, key=lambda i: (-best[i], i))
    rest = [i for i in range(len(cands)) if i not in best]
    return [(cands[i], best[i]) for i in order] + [(cands[i], None) for i in rest]


def _passage(p: sr.Passage, rerank_score: float | None) -> dict:
    out = {
        "item_id": p.item_id,
        "title": p.title,
        "kind": p.kind,
        "chunk_no": p.chunk_no,
        "chunk_sha": p.chunk_sha,
        "text": p.text,
        "score": p.score,
        "rerank_score": rerank_score,
        "source": p.source,
        "corpus": p.corpus,
    }
    if p.corpus == "repodoc":
        out.update(doc_id=p.doc_id, repo=p.repo, path=p.path)
    return out


@router.post(
    "/api/rag/retrieve",
    tags=["rag"],
    responses={
        422: {"description": "invalid request or retrieval mode"},
        503: {"description": "no query embedder, or the embedding gateway is down"},
    },
)
def retrieve(
    body: RetrieveRequest,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict:
    check(principal, "read")
    embed = _query_embedder(request)
    if embed is None:
        raise _unavailable("no query embedder is configured")
    try:
        qvec = sr._embed(embed, [body.query])[0]
    except (sr.EmbeddingError, IndexError):
        raise _unavailable("the embedding gateway is unavailable") from None
    reranker = _reranker(request)
    want_rerank = reranker is not None if body.rerank is None else body.rerank
    use_rerank = want_rerank and reranker is not None
    window = body.candidates if use_rerank else body.k
    store = get_store(request)
    try:
        with store.reader() as conn:
            found = sr.passages(
                conn,
                qvec,
                window,
                body.mode,
                _vis(principal),
                body.kind,
                body.item_ids,
                query=body.query,
                corpora=tuple(dict.fromkeys(body.corpora)),
            )
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail={"detail": str(exc), "code": "invalid"}
        ) from None
    ranked: list[tuple[sr.Passage, float | None]] = [(p, None) for p in found]
    reranked = False
    if use_rerank and found:
        try:
            ranked = _rerank(reranker, body.query, found)
            reranked = True
        except Exception:  # any reranker failure degrades to the original order
            ranked = [(p, None) for p in found]
    return {
        "query": body.query,
        "mode": body.mode,
        "reranked": reranked,
        "passages": [_passage(p, s) for p, s in ranked[: body.k]],
    }


@router.get(
    "/api/rag/items/{item_id}/chunks",
    tags=["rag"],
    responses={404: {"description": "item missing, archived or not visible"}},
)
def item_chunks(
    item_id: int,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict:
    check(principal, "read")
    store = get_store(request)
    vis = _vis(principal)
    try:
        item = library.get(store, item_id, include_private=vis.include_private, owner=vis.owner)
    except NotFoundError:
        raise HTTPException(
            status_code=404, detail={"detail": f"item {item_id} not found", "code": "not_found"}
        ) from None
    with store.reader() as conn:
        chunks = sr.item_chunks(conn, item.id, vis)
        state = conn.execute(
            "SELECT state FROM index_queue WHERE item_id = %s", (item.id,)
        ).fetchone()
        model = conn.execute(
            "SELECT model_id FROM embeddings WHERE item_id = %s ORDER BY chunk_no LIMIT 1",
            (item.id,),
        ).fetchone()
    return {
        "item_id": item.id,
        "item_sha256": item.sha256,
        "index_state": state[0] if state else "none",
        "embed_model": model[0] if model else None,
        "chunks": [
            {
                "chunk_no": c.chunk_no,
                "chunk_sha": c.chunk_sha,
                "text": c.text,
                "is_header": c.is_header,
            }
            for c in chunks
        ],
    }
