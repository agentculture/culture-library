"""Full-text (SQLite FTS5) and semantic (gateway embeddings) search, visibility-aware.

Stdlib only. Titles are indexed synchronously at add time (:func:`index_title`) so a new
item is title-searchable immediately; bodies and embeddings are filled in later by
:mod:`culture_library.core.indexer`. Vectors are stored as float32 BLOBs and ranked by
cosine similarity in Python (fine at this catalog's scale).

The embedder is an injected callable ``texts -> vectors``; the HTTP client for the lobes
gateway lives outside ``core`` in :mod:`culture_library.gateway` (core stays network-free).

Repo documents (devague-work plan task t11, deviations d7 and d8). Search has two *corpora*:
``"item"`` (library items, as before) and ``"repodoc"`` (pushed specs, plans, devague records
and designs from :mod:`culture_library.core.repodocs`). Storage, the smallest change that
keeps the item tables and their foreign keys untouched:

* full text goes in a sibling FTS5 table ``repodoc_fts`` (``rowid`` = ``repodocs.id``), not
  in ``search_fts``. d7 said ``search_fts`` under negative rowids; that was changed because
  FTS5's bm25 uses table-wide statistics (row count, average length), so thousands of repo
  docs in ``search_fts`` would move every item's full-text score and the hybrid ranks the
  eval baseline depends on. A sibling table keeps item scores byte-identical. Its
  ``label`` and ``visibility`` columns are UNINDEXED: the hit title, and the visibility of
  the indexed text (the strictest of the doc's and the shown version's or draft's);
* vectors go in ``repodoc_embeddings`` (``doc_id`` references ``repodocs(id)`` ON DELETE
  CASCADE; otherwise the columns of ``embeddings``);
* the indexer (:mod:`culture_library.core.indexer`) fills both and drops a removed doc's
  rows.

Every query function takes ``corpora`` (default :data:`ITEMS`, items only), so existing
callers and the eval baseline behave exactly as before; ``corpora=("item", "repodoc")``
includes repo docs. :class:`Hit` and :class:`Passage` gained ``corpus`` (``"item"`` by
default) plus ``doc_id``/``repo``/``path``, which are set only for repo docs, whose
``item_id`` is ``None``; ``source`` keeps its meaning (what produced the hit). A repo doc
is visible only to ``include_private`` callers when it is private (no uploader-owner link:
``owner`` never reveals one), and never once removed.
"""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from array import array
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Sequence, Union

from . import repodocs as _repodocs  # noqa: F401 - registers the repodocs tables
from . import uploaders, vector_cache
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

register_schema("""
CREATE VIRTUAL TABLE IF NOT EXISTS repodoc_fts USING fts5(
    title, body, label UNINDEXED, visibility UNINDEXED, tokenize = 'porter unicode61'
);
CREATE TABLE IF NOT EXISTS repodoc_embeddings (
    doc_id INTEGER NOT NULL REFERENCES repodocs(id) ON DELETE CASCADE,
    chunk_no INTEGER NOT NULL,
    text TEXT NOT NULL,
    vec BLOB NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (doc_id, chunk_no)
)
""")

ITEMS: tuple[str, ...] = ("item",)
CORPORA: tuple[str, ...] = ("item", "repodoc")


class EmbeddingError(CoreError):
    """The embedding gateway is unreachable, misconfigured or returned garbage."""


@dataclass(frozen=True)
class Hit:
    item_id: int | None  # None for a repo doc
    title: str
    kind: str
    visibility: str
    score: float
    snippet: str
    source: str  # "fulltext" | "semantic" | "hybrid"
    corpus: str = "item"  # "item" | "repodoc"
    doc_id: int | None = None  # repo docs only, like repo and path
    repo: str | None = None
    path: str | None = None


def _corpora(corpora: Iterable[str]) -> frozenset[str]:
    want = frozenset(corpora)
    unknown = sorted(want - set(CORPORA))
    if unknown:
        raise ValueError(f"unknown corpus {unknown[0]!r} (expected 'item' or 'repodoc')")
    return want


def _key(h: Hit) -> tuple[str, int | None]:
    return (h.corpus, h.doc_id if h.corpus == "repodoc" else h.item_id)


# ---------------------------------------------------------------- vectors / chunks


def pack_vec(vec: Sequence[float]) -> bytes:
    return array("f", vec).tobytes()


def unpack_vec(blob: bytes) -> list[float]:
    arr = array("f")
    arr.frombytes(blob)
    return arr.tolist()


def cosine_scorer(qvec: Sequence[float]) -> Callable[[bytes], float]:
    """A cosine scorer for one query: stored vector blob -> similarity (0.0 for a zero vector).

    ``math.sumprod`` (C, Python 3.12+) over the raw float array replaces the pure-Python
    sums, so a scan of the whole corpus is several times faster (#25); the query's norm is
    computed once. Scores agree with the old formula to within 1e-12.
    """
    q = array("d", qvec)
    qn = math.sqrt(math.sumprod(q, q))

    def score(blob: bytes) -> float:
        v = array("f")
        v.frombytes(blob)
        vn = math.sqrt(math.sumprod(v, v))
        return math.sumprod(q, v) / (qn * vn) if qn and vn else 0.0

    return score


def _entry_scorer(qvec: Sequence[float]) -> Callable[[vector_cache.Entry | None], float | None]:
    """Like :func:`cosine_scorer`, over cached ``(vector, norm)`` entries (``None``: missing)."""
    q = array("d", qvec)
    qn = math.sqrt(math.sumprod(q, q))

    def score(entry: vector_cache.Entry | None) -> float | None:
        if entry is None:
            return None
        v, vn = entry
        return math.sumprod(q, v) / (qn * vn) if qn and vn else 0.0

    return score


def _chunk_text(conn: sqlite3.Connection, table: str, owner: int, chunk_no: int) -> str:
    column = vector_cache.TABLES[table]
    row = conn.execute(
        f"SELECT text FROM {table} WHERE {column} = ? AND chunk_no = ?",  # nosec B608
        (owner, chunk_no),
    ).fetchone()
    return row[0] if row else ""


CHUNK_MAX_CHARS = 1000


def chunk_text(text: str, max_chars: int = CHUNK_MAX_CHARS) -> list[str]:
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


def set_repodoc_text(
    conn: sqlite3.Connection, doc_id: int, title: str, path: str, text: str, visibility: str
) -> None:
    """(Re)write a repo doc's full-text row; ``title`` and ``path`` are both searchable."""
    conn.execute("DELETE FROM repodoc_fts WHERE rowid = ?", (doc_id,))
    conn.execute(
        "INSERT INTO repodoc_fts (rowid, title, body, label, visibility) VALUES (?, ?, ?, ?, ?)",
        (doc_id, f"{title} {path}".strip(), text, title, visibility),
    )


def set_repodoc_visibility(conn: sqlite3.Connection, doc_id: int, visibility: str) -> None:
    conn.execute("UPDATE repodoc_fts SET visibility = ? WHERE rowid = ?", (visibility, doc_id))


def set_repodoc_embeddings(
    conn: sqlite3.Connection, doc_id: int, chunks: Sequence[str], vecs, model: str = ""
) -> None:
    conn.execute("DELETE FROM repodoc_embeddings WHERE doc_id = ?", (doc_id,))
    conn.executemany(
        "INSERT INTO repodoc_embeddings (doc_id, chunk_no, text, vec, model)"
        " VALUES (?, ?, ?, ?, ?)",
        [(doc_id, n, c, pack_vec(v), model) for n, (c, v) in enumerate(zip(chunks, vecs))],
    )


def drop_repodoc(conn: sqlite3.Connection, doc_id: int) -> None:
    """Remove a repo doc from full text and vectors (a removed doc must not be found)."""
    conn.execute("DELETE FROM repodoc_fts WHERE rowid = ?", (doc_id,))
    conn.execute("DELETE FROM repodoc_embeddings WHERE doc_id = ?", (doc_id,))


# ---------------------------------------------------------------- queries


def _fts_query(q: str) -> str:
    terms = re.findall(r"\w+", q, flags=re.UNICODE)
    if not terms:
        return ""
    quoted = [f'"{t}"' for t in terms]
    quoted[-1] += "*"
    return " ".join(quoted)


def _visible(include_private: bool, owner: str | None) -> tuple[str, list[str]]:
    """Live items the caller may see: public, private access, or uploaded by ``owner``."""
    vis, params = uploaders.visible_sql(
        include_private, owner, visibility="i.visibility", item_id="i.id"
    )
    return f"i.archived_at IS NULL AND {vis}", params


def _doc_visible(include_private: bool) -> str:
    """Repo docs the caller may see: not removed, and public unless ``include_private``.

    Both the doc and its indexed text (``f.visibility``) must be public. Repo docs have no
    uploader, so there is no owner clause.
    """
    where = "d.removed_at IS NULL"
    if not include_private:
        where += " AND d.visibility = 'public' AND f.visibility = 'public'"
    return where


def _merge(hits: list[Hit], limit: int) -> list[Hit]:
    return sorted(hits, key=lambda h: -h.score)[:limit]


def fulltext(
    store: Store,
    query: str,
    *,
    include_private: bool = False,
    owner: str | None = None,
    limit: int = 20,
    corpora: Iterable[str] = ITEMS,
) -> list[Hit]:
    """FTS5 search (porter stemming, prefix on the last term) with highlighted snippets.

    With both corpora, item and repo doc hits are merged by score (each corpus is ranked by
    bm25 over its own table).
    """
    want = _corpora(corpora)
    match = _fts_query(query)
    if not match or not want:
        return []
    items = _fulltext_items(store, match, include_private, owner, limit) if "item" in want else []
    if "repodoc" not in want:
        return items
    return _merge(items + _fulltext_docs(store, match, include_private, limit), limit)


def _fulltext_docs(store: Store, match: str, include_private: bool, limit: int) -> list[Hit]:
    sql = (
        "SELECT d.id, f.label, d.kind, f.visibility, bm25(repodoc_fts, 5.0, 1.0) AS rank, "
        "snippet(repodoc_fts, -1, '[', ']', '…', 12) AS snip, d.repo, d.path "
        "FROM repodoc_fts f JOIN repodocs d ON d.id = f.rowid "
        f"WHERE repodoc_fts MATCH ? AND {_doc_visible(include_private)} "  # nosec B608
        "ORDER BY rank LIMIT ?"
    )
    with store.reader() as conn:
        rows = conn.execute(sql, (match, limit)).fetchall()
    return [
        Hit(None, r[1], r[2], r[3], -float(r[4]), r[5], "fulltext", "repodoc", r[0], r[6], r[7])
        for r in rows
    ]


def _fulltext_items(
    store: Store, match: str, include_private: bool, owner: str | None, limit: int
) -> list[Hit]:
    vis, params = _visible(include_private, owner)
    sql = (
        "SELECT i.id, i.title, i.kind, i.visibility, bm25(search_fts, 5.0, 1.0) AS rank, "
        "snippet(search_fts, -1, '[', ']', '…', 12) AS snip "
        "FROM search_fts JOIN items i ON i.id = search_fts.rowid "
        f"WHERE search_fts MATCH ? AND {vis} "  # nosec B608
        "ORDER BY rank LIMIT ?"
    )
    with store.reader() as conn:
        rows = conn.execute(sql, (match, *params, limit)).fetchall()
    return [Hit(r[0], r[1], r[2], r[3], -float(r[4]), r[5], "fulltext") for r in rows]


def semantic(
    store: Store,
    query: str,
    embed: Embedder,
    *,
    include_private: bool = False,
    owner: str | None = None,
    limit: int = 20,
    corpora: Iterable[str] = ITEMS,
) -> list[Hit]:
    """Cosine similarity of the query embedding against stored chunk vectors (best chunk wins).

    Raises :class:`EmbeddingError` when the query cannot be embedded.
    """
    want = _corpora(corpora)
    if not query.strip() or not want:
        return []
    qvec = _embed(embed, [query])[0]
    best: dict[tuple, tuple] = {}  # (corpus, id) -> (score, table, row); first seen keeps place
    with store.reader() as conn:
        score = _entry_scorer(qvec)
        if "item" in want:
            _best_chunks(conn, "embeddings", _item_meta(conn, include_private, owner), score, best)
        if "repodoc" in want:
            _best_chunks(conn, "repodoc_embeddings", _doc_meta(conn, include_private), score, best)
        top = sorted(best.values(), key=lambda b: -b[0])[:limit]
        return [_semantic_hit(conn, *b) for b in top]


def _item_meta(conn: sqlite3.Connection, include_private: bool, owner: str | None) -> list:
    vis, params = _visible(include_private, owner)
    sql = (
        "SELECT 'item', i.id, i.title, i.kind, i.visibility, e.chunk_no, NULL, NULL, e.rowid "
        "FROM embeddings e JOIN items i ON i.id = e.item_id "
        f"WHERE {vis}"  # nosec B608
    )
    return conn.execute(sql, params).fetchall()


def _doc_meta(conn: sqlite3.Connection, include_private: bool) -> list:
    sql = (
        "SELECT 'repodoc', d.id, f.label, d.kind, f.visibility, e.chunk_no, d.repo, d.path,"
        " e.rowid "
        "FROM repodoc_embeddings e JOIN repodocs d ON d.id = e.doc_id "
        "JOIN repodoc_fts f ON f.rowid = d.id "
        f"WHERE {_doc_visible(include_private)}"  # nosec B608
    )
    return conn.execute(sql).fetchall()


def _best_chunks(conn, table: str, rows: list, score, best: dict[tuple, tuple]) -> None:
    """Keep each owner's best-scoring chunk; vectors come from the in-process cache."""
    cached = vector_cache.vectors(conn, table)
    rows.sort(key=lambda r: r[8])  # storage order, so equal scores rank as they always did
    for r in rows:
        s = score(cached.get((r[1], r[5])))
        if s is None:
            continue
        key = (r[0], r[1])
        cur = best.get(key)
        if cur is None or s > cur[0]:
            best[key] = (s, table, r)


def _semantic_hit(conn: sqlite3.Connection, score: float, table: str, r) -> Hit:
    snippet = _chunk_text(conn, table, r[1], r[5])[:200]
    if r[0] == "item":
        return Hit(r[1], r[2], r[3], r[4], score, snippet, "semantic")
    return Hit(None, r[2], r[3], r[4], score, snippet, "semantic", "repodoc", r[1], r[6], r[7])


def hybrid(
    store: Store,
    query: str,
    embed: Embedder,
    *,
    include_private: bool = False,
    owner: str | None = None,
    limit: int = 20,
    corpora: Iterable[str] = ITEMS,
) -> list[Hit]:
    """Reciprocal-rank fusion of full-text and semantic; degrades to full-text if the
    embedder is down."""
    vis = {"include_private": include_private, "owner": owner, "corpora": tuple(corpora)}
    ft = fulltext(store, query, **vis, limit=limit * 2)
    try:
        sem = semantic(store, query, embed, **vis, limit=limit * 2)
    except EmbeddingError:
        return ft[:limit]
    fused: dict[tuple, float] = {}
    hits: dict[tuple, Hit] = {}
    for ranked in (ft, sem):
        for pos, h in enumerate(ranked):
            k = _key(h)
            fused[k] = fused.get(k, 0.0) + 1.0 / (60 + pos)
            if k not in hits or h.source == "fulltext":
                hits[k] = h
    order = sorted(fused, key=lambda k: -fused[k])[:limit]
    return [replace(hits[k], score=fused[k], source="hybrid") for k in order]


# ---------------------------------------------------------------- chunk-level passages (RAG)


class Vis(NamedTuple):
    """Who is asking: the same two inputs every visibility-aware search takes."""

    include_private: bool = False
    owner: str | None = None


VisLike = Union[Vis, "tuple[bool, str | None]", Mapping[str, Any]]


def _as_vis(vis: VisLike) -> Vis:
    if isinstance(vis, Mapping):
        return Vis(bool(vis.get("include_private", False)), vis.get("owner"))
    return Vis(*vis)


@dataclass(frozen=True)
class Passage:
    """One stored chunk, with its full text, for retrieval-augmented generation.

    ``chunk_no`` 0 is the indexer's title/author header; it is returned like any chunk but
    flagged ``is_header`` so generators can skip it.
    """

    item_id: int | None
    title: str
    kind: str
    chunk_no: int
    chunk_sha: str
    text: str
    score: float
    source: str  # "semantic" | "hybrid"
    is_header: bool = False
    corpus: str = "item"  # "item" | "repodoc"; item_id is None for a repo doc
    doc_id: int | None = None
    repo: str | None = None
    path: str | None = None


class Chunk(NamedTuple):
    chunk_no: int
    chunk_sha: str
    text: str
    is_header: bool


def chunk_sha(text: str) -> str:
    """sha256 hex of a stored chunk's text (utf-8): same text, same sha, across re-index."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _filters(
    vis: Vis, kind: str | None, item_ids: Iterable[int] | None
) -> tuple[str, list[Any]] | None:
    """``(where, params)`` for live, visible items narrowed by kind/ids; None if nothing can
    match (an empty ``item_ids``)."""
    where, params = _visible(vis.include_private, vis.owner)
    out: list[Any] = list(params)
    if kind is not None:
        where += " AND i.kind = ?"
        out.append(kind)
    if item_ids is not None:
        ids = sorted({int(x) for x in item_ids})
        if not ids:
            return None
        where += f" AND i.id IN ({','.join('?' * len(ids))})"
        out.extend(ids)
    return where, out


def _fts_item_rank(conn: sqlite3.Connection, query: str, where: str, params: list) -> dict:
    """``item_id -> 0-based FTS rank`` among the filtered, visible items."""
    match = _fts_query(query)
    if not match:
        return {}
    sql = (
        "SELECT i.id FROM search_fts JOIN items i ON i.id = search_fts.rowid "
        f"WHERE search_fts MATCH ? AND {where} "  # nosec B608
        "ORDER BY bm25(search_fts, 5.0, 1.0), i.id"
    )
    rows = conn.execute(sql, (match, *params)).fetchall()
    return {r[0]: pos for pos, r in enumerate(rows)}


def _fts_doc_rank(conn: sqlite3.Connection, query: str, where: str, params: list) -> dict:
    """``doc_id -> 0-based FTS rank`` among the filtered, visible repo docs."""
    match = _fts_query(query)
    if not match:
        return {}
    sql = (
        "SELECT d.id FROM repodoc_fts f JOIN repodocs d ON d.id = f.rowid "
        f"WHERE repodoc_fts MATCH ? AND {where} "  # nosec B608
        "ORDER BY bm25(repodoc_fts, 5.0, 1.0), d.id"
    )
    rows = conn.execute(sql, (match, *params)).fetchall()
    return {r[0]: pos for pos, r in enumerate(rows)}


# A candidate chunk: (corpus order, id, title, kind, chunk_no, text, vec, repo, path). The
# text and vec slots are NULL from SQL: vectors come from the cache, and the text is read
# only for the chunks that make the top k.
_ITEM, _DOC = 0, 1


def _item_rows(conn: sqlite3.Connection, where: str, params: list) -> list[tuple]:
    sql = (
        "SELECT 0, i.id, i.title, i.kind, e.chunk_no, NULL, NULL, NULL, NULL "
        "FROM embeddings e JOIN items i ON i.id = e.item_id "
        f"WHERE {where}"  # nosec B608
    )
    return [tuple(r) for r in conn.execute(sql, params).fetchall()]


def _doc_filters(vis: Vis, kind: str | None) -> tuple[str, list[Any]]:
    where = _doc_visible(vis.include_private)
    params: list[Any] = []
    if kind is not None:
        where += " AND d.kind = ?"
        params.append(kind)
    return where, params


def _doc_rows(conn: sqlite3.Connection, where: str, params: list) -> list[tuple]:
    sql = (
        "SELECT 1, d.id, f.label, d.kind, e.chunk_no, NULL, NULL, d.repo, d.path "
        "FROM repodoc_embeddings e JOIN repodocs d ON d.id = e.doc_id "
        "JOIN repodoc_fts f ON f.rowid = d.id "
        f"WHERE {where}"  # nosec B608
    )
    return [tuple(r) for r in conn.execute(sql, params).fetchall()]


def passages(
    conn: sqlite3.Connection,
    qvec: Sequence[float],
    k: int,
    mode: str,
    vis: VisLike,
    kind: str | None = None,
    item_ids: Iterable[int] | None = None,
    *,
    query: str = "",
    corpora: Iterable[str] = ITEMS,
) -> list[Passage]:
    """The top ``k`` chunks for a query vector the caller already embedded.

    ``semantic`` ranks every visible chunk by cosine; ``hybrid`` adds reciprocal-rank fusion
    (k=60) of each chunk's semantic rank with its item's FTS rank for ``query`` (no FTS
    match, or an empty ``query``, contributes nothing). Visibility is the same rule as
    :func:`semantic`/:func:`fulltext`; archived items never yield. ``kind`` and ``item_ids``
    narrow the candidates before the ``k`` cutoff.

    ``corpora`` defaults to items only. With ``"repodoc"`` included, repo doc chunks compete
    in the same ranking (a repo doc's FTS rank is its rank among repo docs); ``kind`` also
    narrows repo docs, and a given ``item_ids`` narrows to those items, so it excludes repo
    docs. Ties break on (corpus, id, chunk_no), items first, so an items-only call orders
    exactly as before.
    """
    if mode not in ("semantic", "hybrid"):
        raise ValueError(f"unknown passages mode {mode!r} (expected 'semantic' or 'hybrid')")
    want = _corpora(corpora)
    if k <= 0 or not qvec:
        return []
    v = _as_vis(vis)
    rows: list[tuple] = []
    item_flt = doc_flt = None
    if "item" in want:
        item_flt = _filters(v, kind, item_ids)
        if item_flt is not None:
            rows += _item_rows(conn, *item_flt)
    if "repodoc" in want and item_ids is None:
        doc_flt = _doc_filters(v, kind)
        rows += _doc_rows(conn, *doc_flt)
    score = _entry_scorer(qvec)
    cached = {
        _ITEM: vector_cache.vectors(conn, "embeddings") if item_flt else {},
        _DOC: vector_cache.vectors(conn, "repodoc_embeddings") if doc_flt else {},
    }
    scored = [(s, r) for r in rows if (s := score(cached[r[0]].get((r[1], r[4])))) is not None]
    scored.sort(key=_row_order)
    if mode == "hybrid":
        fts = {
            _ITEM: _fts_item_rank(conn, query, *item_flt) if item_flt else {},
            _DOC: _fts_doc_rank(conn, query, *doc_flt) if doc_flt else {},
        }
        scored = _fuse_with_fts(scored, fts)
    return [_passage(s, _with_text(conn, r), mode) for s, r in scored[:k]]


def _with_text(conn: sqlite3.Connection, r: tuple) -> tuple:
    table = "repodoc_embeddings" if r[0] == _DOC else "embeddings"
    return (*r[:5], _chunk_text(conn, table, r[1], r[4]), *r[6:])


def _row_order(s: tuple) -> tuple:
    return (-s[0], s[1][0], s[1][1], s[1][4])


def _fuse_with_fts(scored: list[tuple], fts: dict[int, dict]) -> list[tuple]:
    """Add each chunk's FTS reciprocal-rank term to its semantic one and re-sort."""
    fused = []
    for pos, (_, r) in enumerate(scored):
        score = 1.0 / (60 + pos)
        rank = fts[r[0]].get(r[1])
        if rank is not None:
            score += 1.0 / (60 + rank)
        fused.append((score, r))
    fused.sort(key=_row_order)
    return fused


def _passage(score: float, r: tuple, mode: str) -> Passage:
    is_doc = r[0] == _DOC
    return Passage(
        None if is_doc else r[1],
        r[2],
        r[3],
        r[4],
        chunk_sha(r[5]),
        r[5],
        score,
        mode,
        r[4] == 0,
        "repodoc" if is_doc else "item",
        r[1] if is_doc else None,
        r[7],
        r[8],
    )


def item_chunks(conn: sqlite3.Connection, item_id: int, vis: VisLike) -> list[Chunk]:
    """Every stored chunk of one item in order, or ``[]`` if it is hidden, archived or
    unindexed for this caller."""
    v = _as_vis(vis)
    where, params = _visible(v.include_private, v.owner)
    sql = (
        "SELECT e.chunk_no, e.text FROM embeddings e JOIN items i ON i.id = e.item_id "
        f"WHERE i.id = ? AND {where} ORDER BY e.chunk_no"  # nosec B608
    )
    rows = conn.execute(sql, (item_id, *params)).fetchall()
    return [Chunk(r[0], chunk_sha(r[1]), r[1], r[0] == 0) for r in rows]


def repodoc_chunks(conn: sqlite3.Connection, doc_id: int, vis: VisLike) -> list[Chunk]:
    """Every stored chunk of one repo doc in order, or ``[]`` if it is hidden, removed or
    unindexed for this caller (the repo doc counterpart of :func:`item_chunks`)."""
    where = _doc_visible(_as_vis(vis).include_private)
    sql = (
        "SELECT e.chunk_no, e.text FROM repodoc_embeddings e "
        "JOIN repodocs d ON d.id = e.doc_id JOIN repodoc_fts f ON f.rowid = d.id "
        f"WHERE d.id = ? AND {where} ORDER BY e.chunk_no"  # nosec B608
    )
    rows = conn.execute(sql, (doc_id,)).fetchall()
    return [Chunk(r[0], chunk_sha(r[1]), r[1], r[0] == 0) for r in rows]
