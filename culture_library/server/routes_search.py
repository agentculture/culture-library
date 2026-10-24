"""``GET /api/search``: full-text, semantic or hybrid search (read scope), plus the indexer owner.

``GET /api/search?q=&mode=fulltext|semantic|hybrid&kind=&limit=`` returns
``{query, mode, hits: [{item_id, title, kind, visibility, snippet, score, source}]}``.
Snippets mark matches with ``[ ]``. Private hits appear only for ``Principal.private``
callers and for the key that uploaded them; archived items never appear.

The embedder is ``app.state.embedder`` when set (tests, or a deployment that injects one),
else :meth:`GatewayEmbedder.from_env` (``None`` unless ``GATEWAY_API_KEY`` is set), cached.
``mode`` defaults to ``hybrid`` with an embedder and ``fulltext`` without. ``hybrid`` without
an embedder, or with the gateway down, degrades to full-text (the response ``mode`` is what
was requested/defaulted; ``source`` on each hit says what produced it). ``semantic`` without
an embedder, or when the query cannot be embedded, answers 503 ``semantic_unavailable``.
``kind`` filtering is applied after ranking over a widened candidate window.

Indexer ownership: this module always starts the background :class:`Indexer` (default
``text_for``, i.e. ingest's text extraction) on app startup and stops it on shutdown. With
an embedder configured in the environment it also computes embeddings (and re-opens jobs
finished while none was configured); without ``GATEWAY_API_KEY`` it indexes item bodies for
full-text only.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request

from culture_library.core import KINDS, Store
from culture_library.core import search as sr
from culture_library.core.indexer import Indexer, enqueue_missing, requeue_unembedded
from culture_library.gateway import GatewayEmbedder
from culture_library.server.auth import Principal, check, current_principal, get_store

_lock = threading.Lock()
_indexers: list[Indexer] = []
_UNSET = object()
_FILTER_WINDOW = 200


def start_indexer(store: Store, embedder: sr.Embedder | None | object = _UNSET) -> Indexer:
    """Start (once per store) the background indexer; without an embedder it is full-text only."""
    if embedder is _UNSET:
        embedder = GatewayEmbedder.from_env()
    with _lock:
        for existing in _indexers:
            if existing.store is store:
                return existing
        indexer = Indexer(store, embedder)  # type: ignore[arg-type]
        _indexers.append(indexer)
    indexer.start()
    return indexer


def stop_indexer() -> None:
    """Stop every indexer :func:`start_indexer` started."""
    with _lock:
        running, _indexers[:] = list(_indexers), []
    for indexer in running:
        indexer.stop()


def notify_indexer(store: Store) -> None:
    """Wake the running indexer for ``store`` after an add (never starts one)."""
    with _lock:
        running = [i for i in _indexers if i.store is store]
    for indexer in running:
        indexer.notify()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the indexer on the app's shared store and queue anything not yet indexed."""
    store = getattr(app.state, "store", None)
    if store is None:
        store = app.state.store = Store()
    indexer = start_indexer(store)
    if indexer is not None:
        enqueue_missing(store)
        if getattr(indexer, "embed", None) is not None:
            requeue_unembedded(store)
        indexer.notify()
    try:
        yield
    finally:
        stop_indexer()


router = APIRouter(lifespan=_lifespan)


def _embedder(request: Request) -> sr.Embedder | None:
    state = request.app.state
    if not hasattr(state, "embedder"):
        state.embedder = GatewayEmbedder.from_env()
    return state.embedder


def _unavailable(reason: str) -> HTTPException:
    return HTTPException(status_code=503, detail={"detail": reason, "code": "semantic_unavailable"})


@router.get("/api/search", tags=["search"])
def search(
    request: Request,
    q: str = Query(..., min_length=1),
    mode: Literal["fulltext", "semantic", "hybrid"] | None = None,
    kind: Literal[KINDS] | None = None,  # type: ignore[valid-type]
    limit: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    check(principal, "read")
    store = get_store(request)
    embed = _embedder(request)
    mode = mode or ("hybrid" if embed else "fulltext")
    window = _FILTER_WINDOW if kind else limit
    vis = {"include_private": principal.private, "owner": principal.key_id}
    if mode == "fulltext":
        hits = sr.fulltext(store, q, **vis, limit=window)
    elif mode == "semantic":
        if embed is None:
            raise _unavailable("no embedding gateway is configured")
        try:
            hits = sr.semantic(store, q, embed, **vis, limit=window)
        except sr.EmbeddingError:
            raise _unavailable("the embedding gateway is unavailable") from None
    elif embed is None:
        hits = sr.fulltext(store, q, **vis, limit=window)
    else:
        hits = sr.hybrid(store, q, embed, **vis, limit=window)
    if kind:
        hits = [h for h in hits if h.kind == kind]
    return {
        "query": q,
        "mode": mode,
        "hits": [
            {
                "item_id": h.item_id,
                "title": h.title,
                "kind": h.kind,
                "visibility": h.visibility,
                "snippet": h.snippet,
                "score": h.score,
                "source": h.source,
            }
            for h in hits[:limit]
        ],
    }
