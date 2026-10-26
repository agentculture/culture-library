"""Eval-set generation for books and newspapers (talks to the server only via ``LibraryClient``).

``generate_book_set`` / ``generate_newspaper_set`` return an unsaved ``EvalSet``; the CLI posts it
with ``client.create_set``. ``guard_text_egress`` runs on item metadata before any chunk text is
fetched, so a non-local judge never triggers a text fetch of a non-public item.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .adapter import ChatLLM, EvalAdapter
from .client import LibraryClient
from .config import JudgeConfig, guard_text_egress
from .errors import EvalApiError, EvalError
from .models import Chunk, EvalSet, Sample

MATCH_CHARS = 120
_WS = re.compile(r"\s+")
# Project Gutenberg's licence header/footer: not the book's own text, so never a question source.
_BOILERPLATE = re.compile(r"project gutenberg", re.IGNORECASE)


class GenerateError(EvalError):
    """Generation failed; ``hint`` says what to do about it."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


def _norm(text: str) -> str:
    return _WS.sub(" ", text or "").strip().lower()


def _pick(chunks: list[Chunk], n: int, seed: int) -> list[Chunk]:
    """Deterministic sample of up to ``n`` chunks (by seed), returned in document order."""
    if n < 1:
        raise GenerateError("samples must be >= 1", "pass --samples with a positive number")
    if len(chunks) <= n:
        return list(chunks)
    chosen = sorted(chunks, key=lambda c: _rank(seed, c.chunk_sha))[:n]
    return sorted(chosen, key=lambda c: c.chunk_no)


def _rank(seed: int, key: object) -> str:
    """Seeded, deterministic ordering key (a hash, not a PRNG: sampling, not secrecy)."""
    return hashlib.sha256(f"{seed}:{key}".encode()).hexdigest()


def _prepare(client: LibraryClient, judge: JudgeConfig, item_id: int) -> dict:
    item = client.get_item(item_id)
    guard_text_egress([item], judge)  # metadata only: before any chunk text is fetched
    return item


def _chunker(client: LibraryClient) -> dict:
    try:
        retrieval = client.status().get("retrieval")
    except AttributeError:
        retrieval = None
    return dict(retrieval) if isinstance(retrieval, dict) else {}


def _body_chunks(client: LibraryClient, item_id: int) -> list[Chunk]:
    chunks = [c for c in client.get_chunks(item_id) if not c.is_header]
    if not chunks:
        raise GenerateError(
            f"item {item_id} has no indexed chunks",
            "ingest/index the item first, then retry",
        )
    return chunks


def _book_chunks(chunks: list[Chunk]) -> tuple[list[Chunk], int]:
    """Drop licence boilerplate chunks; ``(kept, skipped)``. Keeps everything if all match."""
    kept = [c for c in chunks if not _BOILERPLATE.search(c.text)]
    if not kept:
        return chunks, 0
    return kept, len(chunks) - len(kept)


def _item_sha(item: dict) -> str:
    return str(item.get("sha256") or item.get("item_sha256") or "")


def _generator(adapter: EvalAdapter, judge: JudgeConfig, samples: int, seed: int, **extra) -> dict:
    return {
        "samples": samples,
        "seed": seed,
        "adapter": adapter.name,
        "judge_model": judge.model,
        **extra,
    }


def generate_book_set(
    client: LibraryClient,
    adapter: EvalAdapter,
    llm: ChatLLM,
    judge: JudgeConfig,
    item_id: int,
    *,
    samples: int = 20,
    seed: int = 0,
) -> EvalSet:
    item = _prepare(client, judge, item_id)
    chunks, boilerplate = _book_chunks(_body_chunks(client, item_id))
    by_sha = {c.chunk_sha: c for c in chunks}
    chosen = _pick(chunks, samples, seed)
    out: list[Sample] = []
    dropped = 0
    for s in adapter.generate_samples(chosen, samples, llm):
        shas = list(s.reference_chunk_shas)
        if not shas or any(sha not in by_sha for sha in shas):
            dropped += 1
            continue
        s.reference_contexts = [by_sha[sha].text for sha in shas]  # verbatim chunk text
        out.append(s)
    if not out:
        raise GenerateError("the adapter produced no usable samples", "check the judge model")
    return EvalSet(
        item_id=int(item_id),
        item_sha256=_item_sha(item),
        adapter=adapter.name,
        adapter_version=adapter.version,
        chunker=_chunker(client),
        generator=_generator(
            adapter, judge, samples, seed, dropped=dropped, skipped_boilerplate=boilerplate
        ),
        samples=out,
    )


def _article_urls(article: dict) -> list[str]:
    urls = article.get("source_urls") or article.get("urls") or article.get("url") or []
    if isinstance(urls, str):
        urls = [urls]
    return [u for u in urls if isinstance(u, str) and u]


def _article_excerpt(article: dict) -> str:
    return str(article.get("excerpt") or article.get("text") or article.get("title") or "")


def _sidecar_articles(client: LibraryClient, item_id: int) -> list[dict]:
    try:
        manifest: Any = client.get_sources(item_id)
    except EvalApiError as exc:
        if exc.status != 404:
            raise
        manifest = None
    articles = [a for a in (manifest or []) if isinstance(a, dict)]
    if not articles:
        raise GenerateError(
            f"newspaper item {item_id} has no sources sidecar",
            f"PUT /api/library/{item_id}/sources with a list of "
            "{article_title, source_urls, excerpt} (culture-rules pushes it with each issue)",
        )
    return articles


def _match_articles(
    articles: list[dict], chunks: list[Chunk]
) -> tuple[list[tuple[dict, list[Chunk]]], int]:
    """(article, matching chunks) for each article with URLs, plus the unmatched count."""
    normalized = [(c, _norm(c.text)) for c in chunks]
    matched: list[tuple[dict, list[Chunk]]] = []
    unmatched = 0
    for art in articles:
        probe = _norm(_article_excerpt(art))[:MATCH_CHARS]
        hits = [c for c, n in normalized if probe and probe in n]
        if hits and _article_urls(art):
            matched.append((art, hits))
        else:
            unmatched += 1
    if not matched:
        raise GenerateError(
            "no sidecar article matched any chunk",
            "check the manifest excerpts against the item text and that each has source_urls",
        )
    return matched, unmatched


def _article_samples(
    adapter: EvalAdapter, llm: ChatLLM, matched: list[tuple[dict, list[Chunk]]]
) -> list[Sample]:
    """One sample per matched article, anchored to its chunks and carrying its URLs."""
    out: list[Sample] = []
    for art, hits in matched:
        by_sha = {c.chunk_sha: c for c in hits}
        for s in adapter.generate_samples(hits, 1, llm)[:1]:
            shas = [sha for sha in s.reference_chunk_shas if sha in by_sha] or [hits[0].chunk_sha]
            s.reference_chunk_shas = shas
            s.reference_contexts = [by_sha[sha].text for sha in shas]
            s.source_urls = _article_urls(art)
            out.append(s)
    return out


def generate_newspaper_set(
    client: LibraryClient,
    adapter: EvalAdapter,
    llm: ChatLLM,
    judge: JudgeConfig,
    item_id: int,
    *,
    samples: int = 20,
    seed: int = 0,
) -> EvalSet:
    item = _prepare(client, judge, item_id)
    articles = _sidecar_articles(client, item_id)
    matched, unmatched = _match_articles(articles, _body_chunks(client, item_id))
    if len(matched) > samples:
        keep = sorted(sorted(range(len(matched)), key=lambda i: _rank(seed, i))[:samples])
        matched = [matched[i] for i in keep]
    out = _article_samples(adapter, llm, matched)
    if not out:
        raise GenerateError("the adapter produced no usable samples", "check the judge model")
    return EvalSet(
        item_id=int(item_id),
        item_sha256=_item_sha(item),
        adapter=adapter.name,
        adapter_version=adapter.version,
        chunker=_chunker(client),
        generator=_generator(adapter, judge, samples, seed, unmatched=unmatched),
        samples=out,
    )
