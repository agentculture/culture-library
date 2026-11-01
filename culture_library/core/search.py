"""Full-text (Postgres tsvector) and semantic (pgvector) search, visibility-aware.

Titles are indexed synchronously at add time (:func:`index_title`) so a new item is
title-searchable immediately; bodies and embeddings are filled in later by
:mod:`culture_library.core.indexer`. The embedder is an injected callable
``texts -> vectors``; the HTTP client for the lobes gateway lives outside ``core`` in
:mod:`culture_library.gateway`, so core stays network-free and the configured gateway is the
only place document text is sent.

Storage (``core/migrations/0001_initial.sql``):

* full text: ``item_text`` (``item_id``; ``title`` holds the item's "title author") and
  ``repodoc_text`` (``doc_id``; ``title`` holds the doc's "title path", ``label`` the display
  title, ``visibility`` the strictest of the doc's and its shown text's). Each has a
  generated ``tsv`` (title weighted A, body weighted B, English stemming) with a GIN index;
* vectors: ``embeddings`` / ``repodoc_embeddings``, keyed by owner, ``model_id``,
  ``strategy`` (:meth:`Strategy.key`) and ``chunk_no``, with one partial HNSW cosine index
  per (model id, strategy) (:func:`culture_library.core.embedding_strategy.hnsw_index_sql`).

Full-text ranking (spec decisions s8 and s21) is ``ts_rank`` over the weighted tsvector, with
weights A=1.0 and B=0.2 (the 5:1 title:body ratio bm25 used) and normalisation 1 (divided by
1 + log of the document length). ``ts_rank`` has no IDF, so one corpus never moves another's
scores. Every query word must match (AND), stemmed; the last word also matches as a prefix.
English stopwords, which ``to_tsquery('english')`` drops, still match titles (and, as the last
word, lexeme prefixes) through a ``'simple'``-config fallback (:class:`_FtsQuery`). Snippets
come from ``ts_headline`` with ``[`` ``]`` markers and about 12 words, computed only for the rows
returned, over one column as the FTS5 snippet was: the body when the stored vector's body
lexemes match, else the title. The body headline reads a bounded window around the first
likely match (:data:`SNIPPET_WINDOW`), never the whole body.

Semantic ranking orders by the pgvector cosine distance ``vec::vector(N) <=> q::vector(N)``
with the (model id, strategy) written as SQL literals, so the planner can prove the partial
HNSW index's predicate (a bound parameter may be planned generically once psycopg prepares a
statement, and a generic plan cannot use a partial index). Rows of another model or strategy
are never scored. pgvector applies the visibility, kind and archived filters after the index
scan, so each index query runs with ``hnsw.iterative_scan = relaxed_order`` (the scan goes on
until enough rows pass the filters, up to ``hnsw.max_scan_tuples``) and the candidates are
re-sorted exactly. A narrow filter, given ``item_ids`` (and the owners full text found, in
hybrid), skips the index: those owners' chunks are scored exactly. The measurements behind
this choice are in ``tests/core/test_search_hnsw.py``.

Two corpora: ``"item"`` (library items) and ``"repodoc"`` (pushed specs, plans, devague
records and designs). Every query function takes ``corpora`` (default :data:`ITEMS`, items
only); :class:`Hit` and :class:`Passage` carry ``corpus`` plus ``doc_id``/``repo``/``path``
for repo docs, whose ``item_id`` is ``None``. A repo doc is visible only to
``include_private`` callers when it is private (``owner`` never reveals one), and never once
removed.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Sequence, Union

from . import uploaders
from .embedding_strategy import CURRENT_MODEL_ID, CURRENT_STRATEGY, Strategy
from .errors import CoreError
from .models import Item
from .store import Store

Embedder = Callable[[Sequence[str]], "list[list[float]]"]

ITEMS: tuple[str, ...] = ("item",)
CORPORA: tuple[str, ...] = ("item", "repodoc")

# ts_rank weights in {D, C, B, A} order: a title (A) match counts five times a body (B) one.
_WEIGHTS = "'{0.0, 0.0, 0.2, 1.0}'"
_HEADLINE = "StartSel=[, StopSel=], MaxWords=12, MinWords=6, MaxFragments=1"
RRF_K = 60
# hybrid passages: the nearest chunks and the top full-text owners per corpus that are fused
HYBRID_POOL = 50
_EF_SEARCH_MAX = 1000  # pgvector's upper limit for hnsw.ef_search
# The least hnsw.ef_search an index query runs with (pgvector default 40): measured in
# tests/core/test_search_hnsw.py, 100 lifts recall@5 on unstructured vectors from 0.51 to 0.80
# for about 2 ms, and clustered (embedding-like) vectors reach 1.00 at either setting.
EF_SEARCH = 100


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


def _key(h: Hit) -> tuple[str, int]:
    oid = h.doc_id if h.corpus == "repodoc" else h.item_id
    return (h.corpus, oid or 0)


# ---------------------------------------------------------------- vectors / chunks


def vector_literal(vec: Sequence[float]) -> str:
    """The pgvector text form of ``vec`` (``[1.0,0.5]``); refuses NaN and infinities."""
    vals = [float(x) for x in vec]
    if not all(math.isfinite(x) for x in vals):
        raise ValueError("a vector must contain only finite numbers")
    return "[" + ",".join(repr(x) for x in vals) + "]"


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _pair_sql(model_id: str, strategy: Strategy) -> str:
    """``e.model_id = '…' AND e.strategy = '…'`` as literals, the HNSW index's predicate."""
    return f"e.model_id = {_lit(model_id)} AND e.strategy = {_lit(strategy.key())}"


def _similarity(dist: float | None) -> float:
    """Cosine similarity from pgvector's distance; 0.0 for a zero vector (NaN distance)."""
    if dist is None or math.isnan(dist):
        return 0.0
    return 1.0 - float(dist)


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


def embedder_space(embed: Embedder) -> tuple[str, Strategy]:
    """``(model_id, strategy)`` of the vectors ``embed`` makes (plan risk r6, decision c42).

    An embedder that states its space (``vector_space()``, as
    :meth:`culture_library.gateway.GatewayEmbedder.vector_space` does from the gateway's
    ``/capabilities``) is asked; the indexer writes under that pair, so a query selecting it
    finds what was indexed. Only an embedder that states no space (a plain callable) gets the
    current pair (:data:`~culture_library.core.embedding_strategy.CURRENT_MODEL_ID`,
    ``CURRENT_STRATEGY``). A failing lookup raises :class:`EmbeddingError`; the pair is never
    guessed.
    """
    lookup = getattr(embed, "vector_space", None)
    if not callable(lookup):
        return CURRENT_MODEL_ID, CURRENT_STRATEGY
    try:
        model_id, strategy = lookup()
    except EmbeddingError:
        raise
    except Exception as exc:  # a custom embedder's lookup failing is the same thing
        raise EmbeddingError(f"cannot resolve the vector space: {exc}") from exc
    return model_id, strategy


def _resolve(embed: Embedder, model_id: str | None, strategy: Strategy | None):
    """The explicit pair when given (both or neither), else :func:`embedder_space`."""
    if (model_id is None) != (strategy is None):
        raise ValueError("pass model_id and strategy together, or neither")
    if model_id is not None and strategy is not None:
        return model_id, strategy
    return embedder_space(embed)


# ---------------------------------------------------------------- index writes


def index_title(conn: Any, item: Item) -> None:
    """Synchronously make ``item`` title/author-searchable, keeping any indexed body.

    Call inside the add transaction; it never touches the network.
    """
    conn.execute(
        "INSERT INTO item_text (item_id, title) VALUES (%s, %s) "
        "ON CONFLICT (item_id) DO UPDATE SET title = excluded.title",
        (item.id, f"{item.title} {item.author}".strip()),
    )


def set_body(conn: Any, item: Item, text: str) -> None:
    """(Re)write an item's full-text row: its "title author" and its extracted body."""
    conn.execute(
        "INSERT INTO item_text (item_id, title, body) VALUES (%s, %s, %s) "
        "ON CONFLICT (item_id) DO UPDATE SET title = excluded.title, body = excluded.body",
        (item.id, f"{item.title} {item.author}".strip(), text),
    )


def _vector_rows(
    owner: int, chunks: Sequence[str], vecs, model_id: str, strategy: Strategy
) -> tuple[str, list[tuple]]:
    if not model_id:
        raise ValueError("model_id is required (the real model, e.g. google/embeddinggemma-2)")
    chunks, vecs = list(chunks), list(vecs)
    if len(chunks) != len(vecs):
        raise ValueError(f"{len(chunks)} chunks but {len(vecs)} vectors")
    key = strategy.key()
    rows = []
    for n, (text, vec) in enumerate(zip(chunks, vecs)):
        if len(vec) != strategy.dimension:
            raise EmbeddingError(
                f"chunk {n} has {len(vec)} dimensions; the strategy expects {strategy.dimension}"
            )
        if not any(vec):  # pgvector's HNSW cosine index skips zero vectors: never findable
            raise EmbeddingError(f"chunk {n} has a zero vector, which has no cosine direction")
        rows.append((owner, model_id, key, n, text, vector_literal(vec)))
    return key, rows


def _replace_vectors(conn: Any, table: str, column: str, key: str, rows, owner, model_id):
    conn.execute(
        f"DELETE FROM {table} WHERE {column} = %s"  # nosec B608 - table/column: two callers
        " AND model_id = %s AND strategy = %s",
        (owner, model_id, key),
    )
    if rows:
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {table} ({column}, model_id, strategy, chunk_no, text, vec)"
                " VALUES (%s, %s, %s, %s, %s, %s::vector)",  # nosec B608 - as above
                rows,
            )


def set_embeddings(
    conn: Any,
    item_id: int,
    chunks: Sequence[str],
    vecs: Sequence[Sequence[float]],
    *,
    model_id: str,
    strategy: Strategy,
) -> None:
    """Replace one item's chunk vectors for one (``model_id``, ``strategy``).

    ``chunks[n]`` and ``vecs[n]`` become chunk ``n`` (chunk 0 is the indexer's title/author
    header). Only this pair's rows are replaced; vectors of other models or strategies stay.
    ``model_id`` is the real model (e.g. ``google/embeddinggemma-2``, never a gateway role
    name). Every vector must have ``strategy.dimension`` values and must not be all zeros
    (else :class:`EmbeddingError`: the HNSW cosine index skips zero vectors). The pair's HNSW
    index (built by :func:`~culture_library.core.embedding_strategy.hnsw_index_sql`) is not
    created here; a query works without it, only by a full scan.
    """
    key, rows = _vector_rows(item_id, chunks, vecs, model_id, strategy)
    _replace_vectors(conn, "embeddings", "item_id", key, rows, item_id, model_id)


def set_repodoc_text(
    conn: Any, doc_id: int, title: str, path: str, text: str, visibility: str
) -> None:
    """(Re)write a repo doc's full-text row; ``title`` and the words of ``path`` are both
    searchable as its title.

    The path is stored as its ``\\w+`` words (``docs specs s md``): Postgres's parser reads a
    whole path as one file lexeme, so ``harbor`` would not find ``docs/mockup-harbor.png``.
    """
    words = " ".join(re.findall(r"\w+", path))
    conn.execute(
        "INSERT INTO repodoc_text (doc_id, title, body, label, visibility)"
        " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (doc_id) DO UPDATE SET"
        " title = excluded.title, body = excluded.body, label = excluded.label,"
        " visibility = excluded.visibility",
        (doc_id, f"{title} {words}".strip(), text, title, visibility),
    )


def set_repodoc_visibility(conn: Any, doc_id: int, visibility: str) -> None:
    conn.execute("UPDATE repodoc_text SET visibility = %s WHERE doc_id = %s", (visibility, doc_id))


def set_repodoc_embeddings(
    conn: Any,
    doc_id: int,
    chunks: Sequence[str],
    vecs: Sequence[Sequence[float]],
    *,
    model_id: str,
    strategy: Strategy,
) -> None:
    """Replace one repo doc's chunk vectors for one (``model_id``, ``strategy``): the repo doc
    counterpart of :func:`set_embeddings`, with the same rules."""
    key, rows = _vector_rows(doc_id, chunks, vecs, model_id, strategy)
    _replace_vectors(conn, "repodoc_embeddings", "doc_id", key, rows, doc_id, model_id)


def drop_repodoc(conn: Any, doc_id: int) -> None:
    """Remove a repo doc from full text and from the vectors of every strategy (a removed doc
    must not be found)."""
    conn.execute("DELETE FROM repodoc_text WHERE doc_id = %s", (doc_id,))
    conn.execute("DELETE FROM repodoc_embeddings WHERE doc_id = %s", (doc_id,))


# ---------------------------------------------------------------- full-text queries


def tsquery_text(q: str) -> str:
    """The ``to_tsquery`` input for a user query: every word ANDed, the last as a prefix.

    Each whitespace-separated piece is single-quoted, so no query text is ever tsquery
    syntax. A piece with punctuation (``search.py``, ``mockup-harbor``) matches either as
    Postgres's parser reads it whole (a file, host or hyphenated word is one lexeme in the
    indexed text too) or as its ``\\w+`` words ANDed. ``""`` when the query has no words.
    """
    return " & ".join(_tsquery_pieces(q))


def _tsquery_pieces(q: str) -> list[str]:
    """:func:`tsquery_text`'s ANDed pieces, one per query word (the last a prefix)."""
    pieces = [(p, re.findall(r"\w+", p)) for p in q.split()]
    pieces = [(p, words) for p, words in pieces if words]
    out = []
    for n, (piece, words) in enumerate(pieces):
        star = ":*" if n == len(pieces) - 1 else ""
        split = " & ".join(f"'{w}'" for w in words) + star
        whole = re.sub(r"['\\\s]+", " ", piece).strip()
        if len(words) == 1 and whole == words[0]:
            out.append(split)
        else:
            out.append(f"('{whole}'{star} | {split})")
    return out


_TITLE_SIMPLE = "to_tsvector('simple', {a}.title)"
_LEXEME = re.compile(r"'((?:[^']|'')*)'")


@dataclass(frozen=True)
class _FtsQuery:
    """How one user query matches, ranks and is highlighted (spec decisions s8 and s21).

    ``to_tsquery('english')`` drops English stopwords, which FTS5 (unicode61/porter) kept, so:

    * normally the stored ``tsv`` is matched and ranked with the english query;
    * when every word is a stopword (``the``, ``a``, ``The The``), the english query is empty
      and the ``'simple'`` query (no stemming, no stopwords) is used instead, against a
      ``to_tsvector('simple', title)`` computed in the query (titles are short; bodies are not
      re-parsed) or the stored ``tsv`` (where the last word's prefix finds ``the`` in
      ``theory``); its rank is over the simple title (weight A) and the stored ``tsv``;
    * when only the last word is a stopword (``road the``), the english query keeps the other
      words and ranks, and the last word must still match as a prefix of a word, as in FTS5:
      of a title word, a stored lexeme, or a body word in ``simple_tsv`` (the unstemmed body,
      migration 0004). The english ``tsv`` never holds the stopword itself, so without
      ``simple_tsv`` ``... ship with?`` would need a ``without`` in the doc (deviations d20,
      d21; d22 moved the body check from a regex to this index).
    """

    cfg: str  # the text search config of ``q``: "english" or "simple"
    q: str  # to_tsquery input for matching, ranking and the headline
    last: str | None  # the simple prefix the last (stopword) word must match, if english lost it
    lexemes: tuple[str, ...]  # the query's lexemes, to place the body snippet's window

    def where(self, a: str) -> tuple[str, list[str]]:
        tsq = f"to_tsquery('{self.cfg}', %s)"
        if self.cfg == "simple":
            return f"({a}.tsv @@ {tsq} OR {_TITLE_SIMPLE.format(a=a)} @@ {tsq})", [self.q] * 2
        if self.last is None:
            return f"{a}.tsv @@ {tsq}", [self.q]
        last = "to_tsquery('simple', %s)"
        return (
            f"{a}.tsv @@ {tsq} AND ({a}.tsv @@ {last} OR {a}.simple_tsv @@ {last}"
            f" OR {_TITLE_SIMPLE.format(a=a)} @@ {last})",
            [self.q, self.last, self.last, self.last],
        )

    def rank(self, a: str) -> tuple[str, list[str]]:
        vector = f"{a}.tsv"
        if self.cfg == "simple":
            vector = f"setweight({_TITLE_SIMPLE.format(a=a)}, 'A') || {a}.tsv"
        return f"ts_rank({_WEIGHTS}, {vector}, to_tsquery('{self.cfg}', %s), 1)", [self.q]


def _fts_query(conn: Any, query: str) -> _FtsQuery | None:
    """The :class:`_FtsQuery` for ``query``; ``None`` when it has no words. One round trip
    asks Postgres which words its english config keeps, and for the query's lexemes."""
    pieces = _tsquery_pieces(query)
    if not pieces:
        return None
    q = " & ".join(pieces)
    english, last_kept, eng_text, simple_text = conn.execute(
        "SELECT numnode(to_tsquery('english', %s)), numnode(to_tsquery('english', %s)),"
        " to_tsquery('english', %s)::text, to_tsquery('simple', %s)::text",
        (q, pieces[-1], q, q),
    ).fetchone()
    if not english:
        return _FtsQuery("simple", q, None, _lexemes(simple_text))
    last = None if last_kept else pieces[-1]
    return _FtsQuery("english", q, last, _lexemes(eng_text))


def _lexemes(tsquery: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(m.replace("''", "'") for m in _LEXEME.findall(tsquery)))


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


# The body snippet's window: ts_headline parses only SNIPPET_WINDOW characters of the body,
# starting SNIPPET_LEAD before the first match. Over a whole 3 MB book the old snippet (a fresh
# to_tsvector of the body, then ts_headline over all of it) cost about 290 ms a hit; the window
# costs about 0.5 ms, and finding it (regexp_instr, then substr, over the body) 10 to 19 ms as
# the match moves from the start to the end of the body: both walk the text up to the match
# (tests/core/test_search.py, test_snippet_cost_is_bounded_for_a_multi_mb_body).
SNIPPET_WINDOW = 1000
SNIPPET_LEAD = 250
# ts_headline marks with these control characters first, so the SQL can tell whether the window
# was marked at all (a body's own "[" would not), then they become [ and ].
_MARKS = "chr(1) || chr(2)"
_BODY_HEADLINE = "StartSel=\x01, StopSel=\x02, MaxWords=12, MinWords=6, MaxFragments=1"
# A space after each run of punctuation inside a token (core/ search. py), so the parser reads
# the words of a punctuated token separately and ts_headline can mark one (migration 0002
# indexes those words; the parser alone reads core/search.py as one file lexeme).
_SPLIT_RE = r"'([^[:space:][:punct:]])([[:punct:]]+)(?=[^[:space:][:punct:]])', '\1\2 ', 'g'"


def _window_locator(lexemes: Iterable[str]) -> str | None:
    """A case-insensitive regex for the first body word that may carry a query lexeme: each
    lexeme at a word start, a trailing ``e``/``i`` dropped first (porter's ``happi`` for
    happy and ``hope`` for hoping are no prefixes of the word). ``None`` without lexemes.

    A false start (``hop`` in hopper) only costs the highlight: ts_headline then shows the
    window's first words unmarked.
    """
    alts = []
    for lex in lexemes:
        if len(lex) > 3 and lex[-1] in "ei":
            lex = lex[:-1]
        alts.append("".join(c if c.isalnum() else "\\" + c for c in lex))
    return r"\m(" + "|".join(alts) + ")" if alts else None


def _headline_sql(fq: _FtsQuery, a: str) -> tuple[str, list]:
    """The snippet from one column, as the FTS5 snippet (column -1, the best column) was:
    the body when the query matches it, else the title.

    Whether the body matches is read from the stored ``tsv``'s weight-B (body) lexemes, which
    include migration 0002's punctuation-split words, so nothing is re-parsed. The body
    headline reads only :data:`SNIPPET_WINDOW` characters around the first likely match
    (:func:`_window_locator`); if nothing in that window was marked, the window is headlined
    again with its punctuated tokens split (:data:`_SPLIT_RE`).
    """
    tsq = f"to_tsquery('{fq.cfg}', %s)"

    def headline(text: str, opts: str) -> str:
        return f"ts_headline('{fq.cfg}', {text}, {tsq}, '{opts}')"

    window = (
        f"translate(substr({a}.body, greatest(1, coalesce(nullif("
        f"regexp_instr({a}.body, %s::text, 1, 1, 0, 'i'), 0), 1) - {SNIPPET_LEAD}),"
        f" {SNIPPET_WINDOW}), {_MARKS}, '')"
    )
    # OFFSET 0 keeps each subquery from being inlined: inlined, the window (and so the body
    # scan) and the headline ran once per reference, about doubling the cost.
    body = (
        f"(SELECT translate(CASE WHEN strpos(h, chr(1)) > 0 THEN h"  # nosec B608 - fixed
        f" ELSE {headline(f'regexp_replace(w, {_SPLIT_RE})', _BODY_HEADLINE)} END,"
        f" {_MARKS}, '[]')"
        f" FROM (SELECT w, {headline('w', _BODY_HEADLINE)} AS h"
        f" FROM (SELECT {window} AS w OFFSET 0) AS win OFFSET 0) AS hl)"
    )  # fixed fragments and a two-value config; the query and locator are bound parameters
    sql = (
        f"CASE WHEN ts_filter({a}.tsv, '{{b}}') @@ {tsq} THEN {body}"
        f" ELSE {headline(f'{a}.title', _HEADLINE)} END"
    )
    return sql, [fq.q, fq.q, fq.q, _window_locator(fq.lexemes), fq.q]


def fulltext(
    store: Store,
    query: str,
    *,
    include_private: bool = False,
    owner: str | None = None,
    limit: int = 20,
    corpora: Iterable[str] = ITEMS,
) -> list[Hit]:
    """Full-text search (English stemming, every word, prefix on the last), ranked by
    ``ts_rank`` with title matches above body matches, with highlighted snippets.

    With both corpora, item and repo doc hits are merged by score; ties break on
    (corpus, id).
    """
    want = _corpora(corpora)
    if not tsquery_text(query) or not want or limit <= 0:
        return []
    hits: list[Hit] = []
    with store.reader() as conn:
        fq = _fts_query(conn, query)
        if fq is None:
            return []
        if "item" in want:
            hits += _fulltext_items(conn, fq, include_private, owner, limit)
        if "repodoc" in want:
            hits += _fulltext_docs(conn, fq, include_private, limit)
    return sorted(hits, key=lambda h: (-h.score, *_key(h)))[:limit]


def _fulltext_items(
    conn: Any, fq: _FtsQuery, include_private: bool, owner: str | None, limit: int
) -> list[Hit]:
    vis, params = _visible(include_private, owner)
    rank, rank_params = fq.rank("t")
    match, match_params = fq.where("t")
    snippet, snippet_params = _headline_sql(fq, "t")
    sql = (
        "WITH hits AS ("  # nosec B608
        f" SELECT i.id, i.title, i.kind, i.visibility, {rank} AS rank"
        " FROM item_text t JOIN items i ON i.id = t.item_id"
        f" WHERE {match} AND {vis}"
        " ORDER BY rank DESC, i.id LIMIT %s)"
        f" SELECT h.id, h.title, h.kind, h.visibility, h.rank, {snippet}"
        " FROM hits h JOIN item_text t ON t.item_id = h.id ORDER BY h.rank DESC, h.id"
    )  # fixed fragments; every value is a bound parameter
    args = (*rank_params, *match_params, *params, limit, *snippet_params)
    rows = conn.execute(sql, args).fetchall()
    return [Hit(r[0], r[1], r[2], r[3], float(r[4]), r[5], "fulltext") for r in rows]


def _fulltext_docs(conn: Any, fq: _FtsQuery, include_private: bool, limit: int) -> list[Hit]:
    rank, rank_params = fq.rank("f")
    match, match_params = fq.where("f")
    snippet, snippet_params = _headline_sql(fq, "f")
    sql = (
        "WITH hits AS ("  # nosec B608
        f" SELECT d.id, f.label, d.kind, f.visibility, {rank} AS rank, d.repo, d.path"
        " FROM repodoc_text f JOIN repodocs d ON d.id = f.doc_id"
        f" WHERE {match} AND {_doc_visible(include_private)}"
        " ORDER BY rank DESC, d.id LIMIT %s)"
        f" SELECT h.id, h.label, h.kind, h.visibility, h.rank, {snippet},"
        " h.repo, h.path"
        " FROM hits h JOIN repodoc_text f ON f.doc_id = h.id ORDER BY h.rank DESC, h.id"
    )  # fixed fragments; every value is a bound parameter
    args = (*rank_params, *match_params, limit, *snippet_params)
    rows = conn.execute(sql, args).fetchall()
    return [
        Hit(None, r[1], r[2], r[3], float(r[4]), r[5], "fulltext", "repodoc", r[0], r[6], r[7])
        for r in rows
    ]


# ---------------------------------------------------------------- vector candidates

# A candidate chunk row: (dist, corpus order, owner id, chunk_no, text, title, kind,
# visibility, repo, path). Corpus order 0 is items and 1 repo docs, so sorting candidates by
# (dist, corpus, owner, chunk_no) breaks ties on (corpus, id, chunk_no), items first.
_ITEM, _DOC = 0, 1

_SOURCES = {
    _ITEM: (
        "embeddings e JOIN items i ON i.id = e.item_id",
        "e.item_id",
        "i.title, i.kind, i.visibility, NULL::text AS repo, NULL::text AS path",
    ),
    _DOC: (
        "repodoc_embeddings e JOIN repodocs d ON d.id = e.doc_id"
        " JOIN repodoc_text f ON f.doc_id = d.id",
        "e.doc_id",
        "f.label AS title, d.kind, f.visibility, d.repo, d.path",
    ),
}
_FTS_SOURCES = {
    _ITEM: ("item_text t JOIN items i ON i.id = t.item_id", "i.id", "t"),
    _DOC: ("repodoc_text f JOIN repodocs d ON d.id = f.doc_id", "d.id", "f"),
}


def _query_vector(qvec: Sequence[float], strategy: Strategy) -> str:
    if len(qvec) != strategy.dimension:
        raise ValueError(
            f"the query vector has {len(qvec)} dimensions; the strategy expects "
            f"{strategy.dimension}"
        )
    return vector_literal(qvec)


def _ann(conn: Any, corpus: int, q: str, flt: tuple[str, list], limit: int, pair) -> list:
    """The ``limit`` nearest visible chunks by cosine distance, through the pair's HNSW index.

    pgvector applies ``flt`` after the index scan, so the scan runs iteratively
    (``relaxed_order``) until ``limit`` rows pass it, and the candidates are then re-sorted
    exactly by distance.
    """
    model_id, strategy = pair
    src, owner, meta = _SOURCES[corpus]
    n = int(strategy.dimension)
    dist = f"(e.vec::vector({n}) <=> %s::vector({n}))"
    where, params = flt
    sql = (
        "WITH c AS MATERIALIZED ("  # nosec B608
        f" SELECT {dist} AS dist, {owner} AS owner, e.chunk_no, e.text, {meta}"
        f" FROM {src} WHERE {_pair_sql(model_id, strategy)} AND {where}"
        f" ORDER BY {dist} LIMIT %s)"
        f" SELECT dist, {corpus}, owner, chunk_no, text, title, kind, visibility, repo, path"
        " FROM c ORDER BY dist, owner, chunk_no"
    )  # fixed fragments and escaped literals; values are bound parameters
    with conn.transaction():
        conn.execute("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)")
        ef = str(min(_EF_SEARCH_MAX, max(EF_SEARCH, limit + 1)))
        conn.execute("SELECT set_config('hnsw.ef_search', %s, true)", (ef,))
        rows = [tuple(r) for r in conn.execute(sql, (q, *params, q, limit + 1)).fetchall()]
    if len(rows) > limit and rows[limit][0] == rows[limit - 1][0]:
        # The LIMIT cut a group of exactly tied chunks (identical vectors). HNSW cannot order
        # them, so which tied chunks made the pool is arbitrary: complete the group exactly so
        # ties always break on (owner, chunk_no), as the SQLite scan did. The group is found
        # by the tied vectors' stored hash (migration 0003, indexed), so only identical vectors
        # are scored, not the whole corpus.
        edge = rows[limit - 1][0]
        hashes = _vector_hashes(conn, corpus, pair, [r for r in rows if r[0] == edge])
        same_vec = (f"{where} AND e.vec_hash = ANY(%s::uuid[])", [*params, hashes])
        tied = _exact(conn, corpus, q, same_vec, pair, limit=None, at_dist=edge)
        rows = sorted({(r[1], r[2], r[3]): r for r in rows + tied}.values(), key=_ann_order)
    return rows[:limit]


# Each corpus's vector table and its owner column (the ``e`` alias of :data:`_SOURCES`).
_VECTOR_TABLES = {_ITEM: ("embeddings", "item_id"), _DOC: ("repodoc_embeddings", "doc_id")}


def _vector_hashes(conn: Any, corpus: int, pair, rows: list) -> list[str]:
    """The distinct ``vec_hash`` of the pooled chunks ``rows`` (owner r[2], chunk_no r[3])."""
    model_id, strategy = pair
    table, owner = _VECTOR_TABLES[corpus]
    sql = (
        f"SELECT DISTINCT e.vec_hash::text FROM {table} e"  # nosec B608 - catalog names
        f" WHERE {_pair_sql(model_id, strategy)}"
        f" AND (e.{owner}, e.chunk_no) IN (SELECT * FROM unnest(%s::bigint[], %s::int[]))"
    )
    keys = ([r[2] for r in rows], [r[3] for r in rows])
    return sorted(r[0] for r in conn.execute(sql, keys).fetchall())


def _ann_order(r: tuple) -> tuple:
    return (r[0], r[2], r[3])


def _exact(
    conn: Any,
    corpus: int,
    q: str,
    flt: tuple[str, list],
    pair,
    *,
    per_owner: int | None = None,
    limit: int | None = None,
    at_dist: float | None = None,
) -> list:
    """Exact cosine scoring of every chunk ``flt`` selects, without the HNSW index.

    For narrow filters (given item ids, or the owners full text found), where an index scan
    that filters afterwards could lose rows. ``per_owner`` keeps each owner's nearest chunks
    only; ``limit`` caps the result; ``at_dist`` keeps only chunks at exactly that distance
    (completing a tie group the index pool cut).
    """
    model_id, strategy = pair
    src, owner, meta = _SOURCES[corpus]
    n = int(strategy.dimension)
    where, params = flt
    sql = (
        "WITH c AS MATERIALIZED ("  # nosec B608
        f" SELECT e.vec::vector({n}) AS vec, {owner} AS owner, e.chunk_no, e.text, {meta}"
        f" FROM {src} WHERE {_pair_sql(model_id, strategy)} AND {where}),"
        f" s AS (SELECT (c.vec <=> %s::vector({n})) AS dist, c.* FROM c),"
        " r AS (SELECT row_number() OVER (PARTITION BY s.owner ORDER BY s.dist, s.chunk_no)"
        " AS rn, s.* FROM s)"
        f" SELECT dist, {corpus}, owner, chunk_no, text, title, kind, visibility, repo, path"
        " FROM r WHERE (%s::int IS NULL OR rn <= %s::int)"
        " AND (%s::float8 IS NULL OR dist = %s::float8)"
        " ORDER BY dist, owner, chunk_no LIMIT %s"
    )  # fixed fragments and escaped literals; values are bound parameters
    rows = conn.execute(sql, (*params, q, per_owner, per_owner, at_dist, at_dist, limit)).fetchall()
    return [tuple(r) for r in rows]


def _fts_rank(conn: Any, corpus: int, query: str, flt: tuple[str, list], limit: int) -> dict:
    """``owner id -> 0-based full-text rank`` among the top ``limit`` filtered owners."""
    fq = _fts_query(conn, query)
    if fq is None:
        return {}
    src, oid, alias = _FTS_SOURCES[corpus]
    where, params = flt
    match, match_params = fq.where(alias)
    rank, rank_params = fq.rank(alias)
    sql = (
        f"SELECT {oid} FROM {src} WHERE {match} AND {where}"  # nosec B608
        f" ORDER BY {rank} DESC, {oid} LIMIT %s"
    )  # fixed fragments; every value is a bound parameter
    rows = conn.execute(sql, (*match_params, *params, *rank_params, limit)).fetchall()
    return {r[0]: pos for pos, r in enumerate(rows)}


def _order(c: tuple) -> tuple:
    d = c[0]
    return (math.inf if d is None or math.isnan(d) else d, c[1], c[2], c[3])


# ---------------------------------------------------------------- semantic / hybrid hits


def semantic(
    store: Store,
    query: str,
    embed: Embedder,
    *,
    include_private: bool = False,
    owner: str | None = None,
    limit: int = 20,
    corpora: Iterable[str] = ITEMS,
    model_id: str | None = None,
    strategy: Strategy | None = None,
) -> list[Hit]:
    """Cosine similarity of the query embedding against the stored chunk vectors of one
    (``model_id``, ``strategy``), best chunk per item or doc; ties break on (corpus, id).

    Without an explicit pair, the pair is the embedder's (:func:`embedder_space`).
    Raises :class:`EmbeddingError` when the query cannot be embedded, its pair cannot be
    resolved, or its embedding does not have the strategy's dimension.
    """
    want = _corpora(corpora)
    if not query.strip() or not want or limit <= 0:
        return []
    model_id, strategy = _resolve(embed, model_id, strategy)
    qvec = _embed(embed, [query])[0]
    try:
        q = _query_vector(qvec, strategy)
    except ValueError as exc:
        raise EmbeddingError(f"the embedder returned an unusable vector: {exc}") from exc
    pair = (model_id, strategy)
    best: list[tuple] = []
    with store.reader() as conn:
        if "item" in want:
            flt = _visible(include_private, owner)
            best += _best_per_owner(conn, _ITEM, q, flt, limit, pair)
        if "repodoc" in want:
            best += _best_per_owner(conn, _DOC, q, (_doc_visible(include_private), []), limit, pair)
    best.sort(key=_order)
    return [_semantic_hit(c) for c in best[:limit]]


def _best_per_owner(conn: Any, corpus: int, q: str, flt, limit: int, pair) -> list:
    """Each owner's nearest chunk, for the ``limit`` nearest owners.

    Asks the index for ``4 * limit`` chunks and doubles until ``limit`` distinct owners are
    found or the candidates run out.
    """
    want = max(4 * limit, 20)
    while True:
        rows = _ann(conn, corpus, q, flt, want, pair)
        best: dict[int, tuple] = {}
        for r in rows:  # sorted by distance: an owner's first row is its nearest chunk
            best.setdefault(r[2], r)
        if len(best) >= limit or len(rows) < want or want >= 16 * _EF_SEARCH_MAX:
            return list(best.values())
        want *= 2


def _semantic_hit(c: tuple) -> Hit:
    score, snippet = _similarity(c[0]), (c[4] or "")[:200]
    if c[1] == _ITEM:
        return Hit(c[2], c[5], c[6], c[7], score, snippet, "semantic")
    return Hit(None, c[5], c[6], c[7], score, snippet, "semantic", "repodoc", c[2], c[8], c[9])


def hybrid(
    store: Store,
    query: str,
    embed: Embedder,
    *,
    include_private: bool = False,
    owner: str | None = None,
    limit: int = 20,
    corpora: Iterable[str] = ITEMS,
    model_id: str | None = None,
    strategy: Strategy | None = None,
) -> list[Hit]:
    """Reciprocal-rank fusion (k=60) of full-text and semantic; degrades to full-text if the
    embedder is down or its pair cannot be resolved (see :func:`semantic`). Ties break on
    (corpus, id)."""
    vis = {"include_private": include_private, "owner": owner, "corpora": tuple(corpora)}
    ft = fulltext(store, query, **vis, limit=limit * 2)
    try:
        pair = {"model_id": model_id, "strategy": strategy}
        sem = semantic(store, query, embed, **vis, **pair, limit=limit * 2)
    except EmbeddingError:
        return ft[:limit]
    fused: dict[tuple, float] = {}
    hits: dict[tuple, Hit] = {}
    for ranked in (ft, sem):
        for pos, h in enumerate(ranked):
            k = _key(h)
            fused[k] = fused.get(k, 0.0) + 1.0 / (RRF_K + pos)
            if k not in hits or h.source == "fulltext":
                hits[k] = h
    order = sorted(fused, key=lambda k: (-fused[k], *k))[:limit]
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
        where += " AND i.kind = %s"
        out.append(kind)
    if item_ids is not None:
        ids = sorted({int(x) for x in item_ids})
        if not ids:
            return None
        where += " AND i.id = ANY(%s)"
        out.append(ids)
    return where, out


def _doc_filters(vis: Vis, kind: str | None) -> tuple[str, list[Any]]:
    where = _doc_visible(vis.include_private)
    params: list[Any] = []
    if kind is not None:
        where += " AND d.kind = %s"
        params.append(kind)
    return where, params


def passages(
    conn: Any,
    qvec: Sequence[float],
    k: int,
    mode: str,
    vis: VisLike,
    kind: str | None = None,
    item_ids: Iterable[int] | None = None,
    *,
    query: str = "",
    corpora: Iterable[str] = ITEMS,
    model_id: str = CURRENT_MODEL_ID,
    strategy: Strategy = CURRENT_STRATEGY,
) -> list[Passage]:
    """The top ``k`` chunks for a query vector the caller already embedded.

    ``semantic`` ranks the visible chunks of one (``model_id``, ``strategy``) by cosine
    distance through its HNSW index (exactly, over those items' chunks, when ``item_ids`` is
    given). ``hybrid`` adds reciprocal-rank fusion (k=60) of each chunk's semantic rank with
    its item's full-text rank for ``query`` (no match, or an empty ``query``, contributes
    nothing). Per corpus, the fused pool is the nearest ``max(2k, 50)`` chunks plus the
    nearest ``k`` chunks of each of the top ``max(2k, 50)`` full-text owners, and a chunk's
    semantic rank is its rank by distance in that pool. Visibility is the same rule as
    :func:`semantic`/:func:`fulltext`; archived items never yield. ``kind`` and ``item_ids``
    narrow the candidates before the ``k`` cutoff.

    ``corpora`` defaults to items only. With ``"repodoc"`` included, repo doc chunks compete
    in the same ranking (a repo doc's full-text rank is its rank among repo docs); ``kind``
    also narrows repo docs, and a given ``item_ids`` narrows to those items, so it excludes
    repo docs. Ties break on (corpus, id, chunk_no), items first. Raises ``ValueError`` for
    an unknown mode or corpus, or a query vector of the wrong dimension.
    """
    if mode not in ("semantic", "hybrid"):
        raise ValueError(f"unknown passages mode {mode!r} (expected 'semantic' or 'hybrid')")
    want = _corpora(corpora)
    if k <= 0 or not qvec:
        return []
    q = _query_vector(qvec, strategy)
    v = _as_vis(vis)
    pair = (model_id, strategy)
    flts = _corpus_filters(want, v, kind, item_ids)
    pool = k if mode == "semantic" else max(2 * k, HYBRID_POOL)
    cands: dict[tuple, tuple] = {}
    fts: dict[int, dict] = {}
    for corpus, flt in flts.items():
        exact = corpus == _ITEM and item_ids is not None  # narrow: score those items exactly
        rows, fts_rank = _corpus_candidates(conn, corpus, q, flt, pair, pool, k, mode, query, exact)
        if fts_rank is not None:
            fts[corpus] = fts_rank
        for r in rows:
            cands.setdefault((r[1], r[2], r[3]), r)
    ranked = sorted(cands.values(), key=_order)
    if mode == "semantic":
        scored = [(_similarity(r[0]), r) for r in ranked]
    else:
        scored = _rrf_scores(ranked, fts)
    return [_passage(s, r, mode) for s, r in scored[:k]]


def _corpus_candidates(
    conn: Any,
    corpus: int,
    q: str,
    flt: tuple[str, list],
    pair,
    pool: int,
    k: int,
    mode: str,
    query: str,
    exact: bool,
) -> tuple[list, dict | None]:
    """One corpus's candidate chunks, plus its full-text owner ranks in hybrid mode.

    The nearest ``pool`` chunks (exactly when ``exact``); in hybrid mode, also the nearest
    ``k`` chunks of each owner full text ranked, so a full-text match outside the nearest
    chunks still gets its boost.
    """
    if exact:
        rows = _exact(conn, corpus, q, flt, pair, limit=pool)
    else:
        rows = _ann(conn, corpus, q, flt, pool, pair)
    if mode != "hybrid":
        return rows, None
    fts_rank = _fts_rank(conn, corpus, query, flt, pool)
    if fts_rank:
        owners = (
            f"{flt[0]} AND {_SOURCES[corpus][1]} = ANY(%s)",
            [*flt[1], list(fts_rank)],
        )
        rows = rows + _exact(conn, corpus, q, owners, pair, per_owner=k)
    return rows, fts_rank


def _corpus_filters(
    want: Iterable[str], v: Vis, kind: str | None, item_ids: Iterable[int] | None
) -> dict[int, tuple[str, list]]:
    flts: dict[int, tuple[str, list]] = {}
    if "item" in want:
        item_flt = _filters(v, kind, item_ids)
        if item_flt is not None:
            flts[_ITEM] = item_flt
    if "repodoc" in want and item_ids is None:
        flts[_DOC] = _doc_filters(v, kind)
    return flts


def _rrf_scores(ranked: list[tuple], fts: dict[int, dict]) -> list[tuple[float, tuple]]:
    scored = []
    for pos, r in enumerate(ranked):
        score = 1.0 / (RRF_K + pos)
        rank = fts[r[1]].get(r[2])
        if rank is not None:
            score += 1.0 / (RRF_K + rank)
        scored.append((score, r))
    scored.sort(key=lambda s: (-s[0], s[1][1], s[1][2], s[1][3]))
    return scored


def _passage(score: float, r: tuple, mode: str) -> Passage:
    is_doc = r[1] == _DOC
    return Passage(
        None if is_doc else r[2],
        r[5],
        r[6],
        r[3],
        chunk_sha(r[4]),
        r[4],
        score,
        mode,
        r[3] == 0,
        "repodoc" if is_doc else "item",
        r[2] if is_doc else None,
        r[8],
        r[9],
    )


def item_chunks(
    conn: Any,
    item_id: int,
    vis: VisLike,
    *,
    model_id: str = CURRENT_MODEL_ID,
    strategy: Strategy = CURRENT_STRATEGY,
) -> list[Chunk]:
    """Every stored chunk of one item for one (``model_id``, ``strategy``), in order, or
    ``[]`` if it is hidden, archived or unindexed for this caller."""
    v = _as_vis(vis)
    where, params = _visible(v.include_private, v.owner)
    sql = (
        "SELECT e.chunk_no, e.text FROM embeddings e "  # nosec B608
        "JOIN items i ON i.id = e.item_id "
        f"WHERE i.id = %s AND {_pair_sql(model_id, strategy)} AND {where} ORDER BY e.chunk_no"
    )  # fixed fragments and escaped literals; values are bound parameters
    rows = conn.execute(sql, (item_id, *params)).fetchall()
    return [Chunk(r[0], chunk_sha(r[1]), r[1], r[0] == 0) for r in rows]


def repodoc_chunks(
    conn: Any,
    doc_id: int,
    vis: VisLike,
    *,
    model_id: str = CURRENT_MODEL_ID,
    strategy: Strategy = CURRENT_STRATEGY,
) -> list[Chunk]:
    """Every stored chunk of one repo doc for one (``model_id``, ``strategy``), in order, or
    ``[]`` if it is hidden, removed or unindexed for this caller (the repo doc counterpart of
    :func:`item_chunks`)."""
    where = _doc_visible(_as_vis(vis).include_private)
    sql = (
        "SELECT e.chunk_no, e.text FROM repodoc_embeddings e "  # nosec B608
        "JOIN repodocs d ON d.id = e.doc_id JOIN repodoc_text f ON f.doc_id = d.id "
        f"WHERE d.id = %s AND {_pair_sql(model_id, strategy)} AND {where} ORDER BY e.chunk_no"
    )  # fixed fragments and escaped literals; values are bound parameters
    rows = conn.execute(sql, (doc_id,)).fetchall()
    return [Chunk(r[0], chunk_sha(r[1]), r[1], r[0] == 0) for r in rows]
