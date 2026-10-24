"""Full-text (SQLite FTS5) and semantic (gateway embeddings) search, visibility-aware.

Stdlib only. Titles are indexed synchronously at add time (:func:`index_title`) so a new
item is title-searchable immediately; bodies and embeddings are filled in later by
:mod:`culture_library.core.indexer`. Vectors are stored as float32 BLOBs and ranked by
cosine similarity in Python (fine at this catalog's scale).

The embedder is an injected callable ``texts -> vectors``; the HTTP client for the lobes
gateway lives outside ``core`` in :mod:`culture_library.gateway` (core stays network-free).
"""

from __future__ import annotations

import math
import re
import sqlite3
from array import array
from dataclasses import dataclass
from typing import Callable, Sequence

from .errors import CoreError
from .models import Item
from .store import Store, register_schema

Embedder = Callable[[Sequence[str]], "list[list[float]]"]

register_schema("""
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    title, body, tokenize = 'porter unicode61'
);
CREATE TABLE IF NOT EXISTS embeddings (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    chunk_no INTEGER NOT NULL,
    text TEXT NOT NULL,
    vec BLOB NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (item_id, chunk_no)
)
""")


class EmbeddingError(CoreError):
    """The embedding gateway is unreachable, misconfigured or returned garbage."""


@dataclass(frozen=True)
class Hit:
    item_id: int
    title: str
    kind: str
    visibility: str
    score: float
    snippet: str
    source: str  # "fulltext" | "semantic" | "hybrid"


# ---------------------------------------------------------------- vectors / chunks


def pack_vec(vec: Sequence[float]) -> bytes:
    return array("f", vec).tobytes()


def unpack_vec(blob: bytes) -> list[float]:
    arr = array("f")
    arr.frombytes(blob)
    return arr.tolist()


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def chunk_text(text: str, max_chars: int = 1000) -> list[str]:
    """Split on blank lines, packing paragraphs up to ``max_chars``; hard-split long runs."""
    chunks: list[str] = []
    cur = ""

    def flush() -> None:
        nonlocal cur
        if cur.strip():
            chunks.append(cur.strip())
        cur = ""

    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        while len(para) > max_chars:
            cut = para.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            flush()
            chunks.append(para[:cut].strip())
            para = para[cut:].strip()
        if len(cur) + len(para) + 2 > max_chars:
            flush()
        cur = f"{cur}\n\n{para}" if cur else para
    flush()
    return [c for c in chunks if c]


# ---------------------------------------------------------------- embedder seam


def _embed(embed: Embedder, texts: Sequence[str]) -> list[list[float]]:
    try:
        return embed(texts)
    except EmbeddingError:
        raise
    except Exception as exc:  # any transport failure from a custom embedder
        raise EmbeddingError(f"embedding failed: {exc}") from exc


# ---------------------------------------------------------------- index writes


def index_title(conn: sqlite3.Connection, item: Item) -> None:
    """Synchronously make ``item`` title/author-searchable, keeping any indexed body.

    Call inside the add transaction; it never touches the network.
    """
    row = conn.execute("SELECT body FROM search_fts WHERE rowid = ?", (item.id,)).fetchone()
    body = row[0] if row else ""
    conn.execute("DELETE FROM search_fts WHERE rowid = ?", (item.id,))
    conn.execute(
        "INSERT INTO search_fts (rowid, title, body) VALUES (?, ?, ?)",
        (item.id, f"{item.title} {item.author}".strip(), body),
    )


def set_body(conn: sqlite3.Connection, item: Item, text: str) -> None:
    conn.execute("DELETE FROM search_fts WHERE rowid = ?", (item.id,))
    conn.execute(
        "INSERT INTO search_fts (rowid, title, body) VALUES (?, ?, ?)",
        (item.id, f"{item.title} {item.author}".strip(), text),
    )


def set_embeddings(
    conn: sqlite3.Connection, item_id: int, chunks: Sequence[str], vecs, model: str = ""
) -> None:
    conn.execute("DELETE FROM embeddings WHERE item_id = ?", (item_id,))
    conn.executemany(
        "INSERT INTO embeddings (item_id, chunk_no, text, vec, model) VALUES (?, ?, ?, ?, ?)",
        [(item_id, n, c, pack_vec(v), model) for n, (c, v) in enumerate(zip(chunks, vecs))],
    )


# ---------------------------------------------------------------- queries


def _fts_query(q: str) -> str:
    terms = re.findall(r"\w+", q, flags=re.UNICODE)
    if not terms:
        return ""
    quoted = [f'"{t}"' for t in terms]
    quoted[-1] += "*"
    return " ".join(quoted)


def _visible(include_private: bool) -> str:
    base = "i.archived_at IS NULL"
    return base if include_private else base + " AND i.visibility = 'public'"


def fulltext(
    store: Store, query: str, *, include_private: bool = False, limit: int = 20
) -> list[Hit]:
    """FTS5 search (porter stemming, prefix on the last term) with highlighted snippets."""
    match = _fts_query(query)
    if not match:
        return []
    sql = (
        "SELECT i.id, i.title, i.kind, i.visibility, bm25(search_fts, 5.0, 1.0) AS rank, "
        "snippet(search_fts, -1, '[', ']', '…', 12) AS snip "
        "FROM search_fts JOIN items i ON i.id = search_fts.rowid "
        f"WHERE search_fts MATCH ? AND {_visible(include_private)} "  # nosec B608
        "ORDER BY rank LIMIT ?"
    )
    with store.reader() as conn:
        rows = conn.execute(sql, (match, limit)).fetchall()
    return [Hit(r[0], r[1], r[2], r[3], -float(r[4]), r[5], "fulltext") for r in rows]


def semantic(
    store: Store,
    query: str,
    embed: Embedder,
    *,
    include_private: bool = False,
    limit: int = 20,
) -> list[Hit]:
    """Cosine similarity of the query embedding against stored chunk vectors (best chunk wins).

    Raises :class:`EmbeddingError` when the query cannot be embedded.
    """
    if not query.strip():
        return []
    qvec = _embed(embed, [query])[0]
    sql = (
        "SELECT i.id, i.title, i.kind, i.visibility, e.text, e.vec "
        "FROM embeddings e JOIN items i ON i.id = e.item_id "
        f"WHERE {_visible(include_private)}"  # nosec B608
    )
    with store.reader() as conn:
        rows = conn.execute(sql).fetchall()
    best: dict[int, Hit] = {}
    for r in rows:
        score = _cosine(qvec, unpack_vec(r[5]))
        cur = best.get(r[0])
        if cur is None or score > cur.score:
            best[r[0]] = Hit(r[0], r[1], r[2], r[3], score, r[4][:200], "semantic")
    return sorted(best.values(), key=lambda h: -h.score)[:limit]


def hybrid(
    store: Store,
    query: str,
    embed: Embedder,
    *,
    include_private: bool = False,
    limit: int = 20,
) -> list[Hit]:
    """Reciprocal-rank fusion of full-text and semantic; degrades to full-text if the
    embedder is down."""
    ft = fulltext(store, query, include_private=include_private, limit=limit * 2)
    try:
        sem = semantic(store, query, embed, include_private=include_private, limit=limit * 2)
    except EmbeddingError:
        return ft[:limit]
    fused: dict[int, float] = {}
    hits: dict[int, Hit] = {}
    for ranked in (ft, sem):
        for pos, h in enumerate(ranked):
            fused[h.item_id] = fused.get(h.item_id, 0.0) + 1.0 / (60 + pos)
            if h.item_id not in hits or h.source == "fulltext":
                hits[h.item_id] = h
    order = sorted(fused, key=lambda k: -fused[k])[:limit]
    return [
        Hit(
            hits[k].item_id,
            hits[k].title,
            hits[k].kind,
            hits[k].visibility,
            fused[k],
            hits[k].snippet,
            "hybrid",
        )
        for k in order
    ]
