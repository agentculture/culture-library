"""Background indexer: durable queue, retries with exponential backoff, status data.

Adding an item never depends on this module's work: the API calls :func:`index_on_add`
inside the add transaction (title index + queue row, no network; an upload also passes the
text ingest already extracted, indexed there with the job marked body-done, so its job only
embeds). The :class:`Indexer` otherwise fills in the body full-text row (no gateway needed:
text comes from :func:`culture_library.core.ingest.extract_text`, so EPUB and PDF bodies are
searchable) and the embeddings (gateway). Without an embedder a job completes after its body
(full-text only, not a failure); :func:`requeue_unembedded` re-opens those jobs once an
embedder is configured. A gateway outage only delays embeddings; the job is retried with
backoff and caught up once the gateway is back. :func:`status` exposes lag/failures/last
success as plain data for the status endpoint.

Repo documents (devague-work plan task t11, deviation d7). The push route never calls this
module: each :meth:`Indexer.run_once` *discovers* repo docs (:func:`discover_repodocs`)
whose shown sha256 differs from the one their job holds, and (re)opens their job. Only
docs marked in ``repodoc_dirty`` are considered (#39): plpgsql triggers
(``core/migrations/0002_*.sql``) mark a doc when a write changes what it shows (see
:data:`DIRTY_SOURCES`), and each new :class:`Indexer` and :func:`requeue_all` mark every doc
once, so drift is repaired on the next start. The shown text is the working draft when it is
ahead of the newest version, else the current version (or the newest one while the doc has
no current). A removed doc is never enqueued, and a doc removed after indexing is dropped
from ``repodoc_text``, ``repodoc_embeddings`` and its job row. Text: ``.json`` goes through
:func:`culture_library.core.devague_text.text_for`, Markdown and plain text are indexed raw,
and anything else (binary design assets: images, HTML, PDF) indexes only its path and title
(:func:`repodoc_text`).

Storage (``core/migrations/0001_initial.sql``): item jobs in ``index_queue``, repo doc jobs
in ``repodoc_index_queue`` (keyed by ``doc_id``, with the shown sha256 and the strictest
visibility of the doc and its shown text), both with the same ``pending``/``done``/``failed``
state machine, attempts and exponential backoff. ``index_queue.priority`` orders item jobs
(lower runs first, items default to :data:`PRIORITY_ITEM`). Repo doc jobs are a lower tier
than any item: ``run_once`` runs due item jobs first, and stops the repo doc backlog as soon
as an item job is due, so an upload during the initial backfill is indexed next. A gateway
failure on a repo doc job backs off and retries exactly like an item job; its full text is
already written by then.

Vectors (decision c42, plan task t13): every vector carries the real model id and the
strategy (prompts, chunker and ``max_chars``, dimension, normalisation) it was made with;
see :class:`Indexer` for where the pair comes from, and
:func:`ensure_hnsw_index_concurrently` for the per-pair HNSW index the first vector of a pair
creates (built concurrently, before the job's write transaction).
"""

from __future__ import annotations

import json
import posixpath
import threading
import time
from contextlib import suppress
from typing import Any, Callable

from . import devague_text, ingest, search
from .embedding_strategy import Strategy, hnsw_index_name, hnsw_index_sql
from .errors import ValidationError
from .models import Item
from .store import Store

PRIORITY_ITEM = 0  # index_queue.priority: lower runs first; repo doc jobs run after all items

# The chunker the indexer uses (search.chunk_text: blank-line paragraphs packed up to
# max_chars), as the strategy names it.
CHUNKER = "paragraph-pack"

# pgvector's HNSW index takes at most 2,000 dimensions of a ``vector``; a wider strategy's
# vectors are stored and searched without one (an exact scan).
HNSW_MAX_DIMENSION = 2000
# Advisory lock key serialising HNSW index creation ("culture-library hnsw"): taken for the
# transaction by ensure_hnsw_index, for the session (try, never wait) by the concurrent build.
HNSW_LOCK_KEY = 0x6C6962_686E7377
# How long a concurrent HNSW build waits, in seconds, for each open transaction on the table
# before it gives up (its lock waits obey lock_timeout); the job then backs off and retries.
HNSW_BUILD_LOCK_TIMEOUT = 60.0

# A write that changes what a repo doc shows marks the doc in ``repodoc_dirty`` in the same
# transaction (#39), so discovery plans only those docs and an idle tick never reads
# ``repodocs``. The marks are plpgsql triggers (core/migrations/0002_*.sql): INSERT and
# DELETE always mark, an UPDATE only when one of these columns, the ones ``_SHOWN_SQL`` reads,
# changes. tests/core/test_repodoc_dirty.py checks the triggers watch exactly these, so a new
# ``_SHOWN_SQL`` column needs a migration that widens its trigger.
DIRTY_SOURCES = (  # (table, its doc id column, the columns the plan reads)
    ("repodocs", "id", ("visibility", "current_version_id", "removed_at")),
    ("repodoc_drafts", "doc_id", ("sha256", "visibility")),
    ("repodoc_versions", "doc_id", ("sha256", "visibility", "committed_at")),
)


def strategy_for(
    *,
    document_prompt: str | None,
    query_prompt: str | None,
    dimension: int,
    normalisation: str,
) -> Strategy:
    """The strategy of vectors this indexer makes: the prompts (``""`` for none), its chunker
    and ``search.CHUNK_MAX_CHARS``, and the model's dimension and normalisation."""
    return Strategy(
        document_prompt=document_prompt or "",
        query_prompt=query_prompt or "",
        chunker=CHUNKER,
        max_chars=search.CHUNK_MAX_CHARS,
        dimension=int(dimension),
        normalisation=normalisation,
    )


def mark_all_repodocs(conn: Any) -> None:
    """Mark every repo doc for discovery: the next run re-plans them all (a full sweep)."""
    conn.execute(
        "INSERT INTO repodoc_dirty (doc_id) SELECT id FROM repodocs ON CONFLICT DO NOTHING"
    )


def _relation_exists(conn: Any, name: str) -> bool:
    # An MVCC read of pg_class, not to_regclass() (see core/migrations/__init__.py).
    return conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class WHERE relname = %s"
        " AND relnamespace = current_schema()::regnamespace)",
        (name,),
    ).fetchone()[0]


def ensure_hnsw_index(conn: Any, table: str, model_id: str, strategy: Strategy) -> bool:
    """Create the (``model_id``, ``strategy``) HNSW index on ``table`` in ``conn``'s
    transaction, if it is missing: a plain ``CREATE INDEX``.

    For a bulk load that owns the database (the SQLite migration). Its SHARE lock holds every
    other writer of ``table`` until the transaction commits, and the build scans the whole
    table, so the running :class:`Indexer` uses :func:`ensure_hnsw_index_concurrently`
    instead. An advisory lock serialises two creators, which would otherwise race on the
    index name. Returns True when it created the index. A strategy wider than
    :data:`HNSW_MAX_DIMENSION` gets none.
    """
    if strategy.dimension > HNSW_MAX_DIMENSION:
        return False
    name = hnsw_index_name(table, model_id, strategy)
    if _relation_exists(conn, name):
        return False
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (HNSW_LOCK_KEY,))
    if _relation_exists(conn, name):  # a racing creator committed it first
        return False
    conn.execute(hnsw_index_sql(table, model_id, strategy))
    return True


def _index_valid(conn: Any, name: str) -> bool | None:
    """True for a valid index ``name``, False for an invalid one (a failed concurrent build),
    None when there is none."""
    row = conn.execute(
        "SELECT i.indisvalid FROM pg_catalog.pg_index i"
        " JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid"
        " WHERE c.relname = %s AND c.relnamespace = current_schema()::regnamespace",
        (name,),
    ).fetchone()
    return None if row is None else bool(row[0])


def ensure_hnsw_index_concurrently(
    store: Store,
    table: str,
    model_id: str,
    strategy: Strategy,
    *,
    lock_timeout: float = HNSW_BUILD_LOCK_TIMEOUT,
) -> bool:
    """Build the (``model_id``, ``strategy``) HNSW index on ``table`` without blocking writers.

    The :class:`Indexer` calls it before its first write transaction for a pair, so the first
    vector of a new pair still creates its index, but outside that transaction:
    ``CREATE INDEX CONCURRENTLY`` on a connection of its own in autocommit mode. It takes
    SHARE UPDATE EXCLUSIVE, which no row writer conflicts with, so every other writer of
    ``table`` (another pair's vectors, another job) carries on while it builds.

    Only one session builds: the session advisory lock :data:`HNSW_LOCK_KEY` is *tried*, and
    when another session holds it this returns False at once rather than wait (a waiter would
    hold a snapshot the build itself waits for, a deadlock). Vectors written meanwhile are
    picked up by that build. An INVALID index of the same name, left by a build that failed
    or was cancelled, is dropped (``CONCURRENTLY``) and built again. The build's waits for
    open transactions on ``table`` obey ``lock_timeout`` seconds; past it the error
    propagates (the job backs off) and the invalid index it leaves is rebuilt next time.

    Returns True when the index is valid on return, False when there is none: another session
    is building it, or the strategy is wider than :data:`HNSW_MAX_DIMENSION`.
    """
    if strategy.dimension > HNSW_MAX_DIMENSION:
        return False
    name = hnsw_index_name(table, model_id, strategy)
    with store.reader() as conn:
        if _index_valid(conn, name):
            return True
    create = hnsw_index_sql(table, model_id, strategy)
    prefix = "CREATE INDEX "
    if not create.startswith(prefix):  # pragma: no cover - hnsw_index_sql's fixed shape
        raise ValueError(f"unexpected index DDL: {create[:40]!r}")
    create = "CREATE INDEX CONCURRENTLY " + create[len(prefix) :]
    # A fresh connection from the store, switched to autocommit before its first statement
    # (CONCURRENTLY cannot run in a transaction block), and switched back before it is
    # handed back.
    with store.connect() as conn:
        conn.autocommit = True
        try:
            if not conn.execute("SELECT pg_try_advisory_lock(%s)", (HNSW_LOCK_KEY,)).fetchone()[0]:
                return False
            try:
                conn.execute(
                    "SELECT set_config('lock_timeout', %s, false)",
                    (f"{max(1, int(lock_timeout * 1000))}ms",),
                )
                state = _index_valid(conn, name)
                if state:  # a racing builder finished it
                    return True
                if state is False:
                    conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
                conn.execute(create)
                return True
            finally:  # never mask the build's error; a broken connection drops both anyway
                with suppress(Exception):
                    conn.execute("SELECT pg_advisory_unlock(%s)", (HNSW_LOCK_KEY,))
                    conn.execute("RESET lock_timeout")
        finally:
            with suppress(Exception):
                conn.autocommit = False


TextFor = Callable[[Item], str]


def default_text_for(store: Store, limits: ingest.IngestLimits = ingest.DEFAULT_LIMITS) -> TextFor:
    """Plain text via :func:`ingest.extract_text` (EPUB, PDF via pdftotext, md, txt, json).

    Unsupported formats and content that cannot be extracted (bad encoding, broken EPUB)
    index as an empty body: retrying would not change the bytes. A PDF timeout is raised so
    the job is retried with backoff.
    """

    def text_for(item: Item) -> str:
        fmt = item.format.lower()
        if fmt not in ingest.SUPPORTED_FORMATS:
            return ""
        try:
            return ingest.extract_text(store.read_bytes(item.id), fmt, limits)
        except ValidationError as exc:
            if exc.code == "conversion_timeout":
                raise
            return ""

    return text_for


def enqueue(conn: Any, item_id: int, *, now: float | None = None) -> None:
    conn.execute(
        "INSERT INTO index_queue (item_id, enqueued_at) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (item_id, time.time() if now is None else now),
    )


def index_on_add(conn: Any, item: Item, body: str | None = None) -> None:
    """Hook for the API's add transaction: title searchable now, body + vectors queued.

    ``body`` is text the caller already extracted (an upload's :func:`ingest.ingest`): it is
    indexed now and the job marked ``body_done``, so the indexer only computes embeddings.
    """
    if body is None:
        search.index_title(conn, item)
    else:
        search.set_body(conn, item, body)
    enqueue(conn, item.id)
    if body is not None:
        conn.execute("UPDATE index_queue SET body_done = true WHERE item_id = %s", (item.id,))


def enqueue_missing(store: Store) -> int:
    """Queue items that have no queue row (pre-existing data, migrations)."""
    with store.connect() as conn:
        cur = conn.execute(
            "INSERT INTO index_queue (item_id, enqueued_at) SELECT i.id, %s FROM items i"
            " WHERE NOT EXISTS (SELECT 1 FROM index_queue q WHERE q.item_id = i.id)"
            " ON CONFLICT DO NOTHING",
            (time.time(),),
        )
        n = cur.rowcount
        for row in conn.execute(
            "SELECT i.* FROM items i"
            " WHERE NOT EXISTS (SELECT 1 FROM item_text t WHERE t.item_id = i.id)"
        ).fetchall():
            search.index_title(conn, Item(**dict(row)))
    return n


def requeue_failed(store: Store) -> int:
    """Re-open failed jobs, items and repo docs; return how many."""
    with store.connect() as conn:
        n = 0
        for table in ("index_queue", "repodoc_index_queue"):
            n += conn.execute(
                f"UPDATE {table} SET state='pending', attempts=0, next_attempt_at=0 "  # nosec B608
                "WHERE state='failed'"
            ).rowcount
        return n


def requeue_unembedded(store: Store) -> int:
    """Re-open finished jobs that have no embeddings (indexed while no embedder was set).

    The body stays indexed (``body_done`` is kept), so only the embeddings are computed.
    """
    with store.connect() as conn:
        cur = conn.execute(
            "UPDATE index_queue q SET state='pending', attempts=0, next_attempt_at=0 "
            "WHERE state='done' AND NOT EXISTS "
            "(SELECT 1 FROM embeddings e WHERE e.item_id = q.item_id)"
        )
        docs = conn.execute(
            "UPDATE repodoc_index_queue q SET state='pending', attempts=0, next_attempt_at=0 "
            "WHERE state='done' AND NOT EXISTS "
            "(SELECT 1 FROM repodoc_embeddings e WHERE e.doc_id = q.doc_id)"
        )
        return cur.rowcount + docs.rowcount


def requeue_stale_model(store: Store, model_id: str, strategy: Strategy) -> int:
    """Re-open finished jobs that have no vectors of (``model_id``, ``strategy``).

    That is every job embedded only under another model or strategy, and every body-only job
    (it includes :func:`requeue_unembedded`). The :class:`Indexer` runs it once, on its first
    run with an embedder, for the pair it writes, so a model or prompt change re-embeds the
    corpus under the new pair. The old pair's vectors stay until something drops them.
    """
    if not model_id:
        return 0
    key = strategy.key()
    with store.connect() as conn:
        cur = conn.execute(
            "UPDATE index_queue q SET state='pending', attempts=0, next_attempt_at=0 "
            "WHERE state='done' AND NOT EXISTS (SELECT 1 FROM embeddings e"
            " WHERE e.item_id = q.item_id AND e.model_id = %s AND e.strategy = %s)",
            (model_id, key),
        )
        docs = conn.execute(
            "UPDATE repodoc_index_queue q SET state='pending', attempts=0, next_attempt_at=0 "
            "WHERE state='done' AND NOT EXISTS (SELECT 1 FROM repodoc_embeddings e"
            " WHERE e.doc_id = q.doc_id AND e.model_id = %s AND e.strategy = %s)",
            (model_id, key),
        )
        return cur.rowcount + docs.rowcount


def requeue_all(store: Store) -> int:
    """Re-run every job (e.g. after the text extractor improves)."""
    with store.connect() as conn:
        cur = conn.execute(
            "UPDATE index_queue SET state='pending', body_done=false, attempts=0,"
            " next_attempt_at=0"
        )
        docs = conn.execute(
            "UPDATE repodoc_index_queue SET state='pending', attempts=0, next_attempt_at=0"
        )
        mark_all_repodocs(conn)  # and re-plan every doc, including any with no job row
        return cur.rowcount + docs.rowcount


def _meta_set(conn: Any, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO index_meta (key, value) VALUES (%s, %s)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def status(store: Store, *, now: float | None = None) -> dict:
    """Index health as data: counts, lag (age of the oldest unfinished job), last success."""
    now = time.time() if now is None else now
    with store.reader() as conn:
        counts = {
            r[0]: r[1] for r in conn.execute("SELECT state, COUNT(*) FROM index_queue GROUP BY 1")
        }
        oldest = conn.execute(
            "SELECT MIN(enqueued_at) FROM index_queue WHERE state = 'pending'"
        ).fetchone()[0]
        meta = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM index_meta")}
    return {
        "pending": counts.get("pending", 0),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "lag_seconds": 0 if oldest is None else max(0, int(now - oldest)),
        "last_success_at": float(meta["last_success_at"]) if "last_success_at" in meta else None,
        "last_failure_at": float(meta["last_failure_at"]) if "last_failure_at" in meta else None,
        "last_error": meta.get("last_error"),
    }


def repodoc_status(store: Store, *, now: float | None = None) -> dict:
    """Repo doc job counts and lag, the counterpart of :func:`status` (which counts items)."""
    now = time.time() if now is None else now
    with store.reader() as conn:
        counts = {
            r[0]: r[1]
            for r in conn.execute("SELECT state, COUNT(*) FROM repodoc_index_queue GROUP BY 1")
        }
        oldest = conn.execute(
            "SELECT MIN(enqueued_at) FROM repodoc_index_queue WHERE state = 'pending'"
        ).fetchone()[0]
    return {
        "pending": counts.get("pending", 0),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "lag_seconds": 0 if oldest is None else max(0, int(now - oldest)),
    }


# ---------------------------------------------------------------- repo documents

_TEXT_SUFFIXES = (".md", ".markdown", ".txt", ".rst")


def _json_title(parsed: object) -> str:
    """The first non-blank ``title``/``slug``/``plan_slug`` string of a parsed JSON object."""
    if not isinstance(parsed, dict):
        return ""
    for key in ("title", "slug", "plan_slug"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _json_repodoc_text(path: str, name: str, data: bytes) -> tuple[str, str]:
    raw = data.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw)
    except ValueError:
        return name, raw
    return _json_title(parsed) or name, devague_text.text_for(path, parsed)


def _text_repodoc_text(name: str, raw: str) -> tuple[str, str]:
    for line in raw.splitlines():
        if line.startswith("# ") and line[2:].strip():
            return line[2:].strip(), raw
    return name, raw


def strip_nul(text: str) -> str:
    """``text`` without NUL characters, which a Postgres ``text`` value cannot hold.

    Stripped rather than replaced with U+FFFD: a NUL carries no meaning in prose, and the
    commonest source (UTF-16 text pushed as bytes, a NUL beside every letter) reads, and is
    found, as the right words once they are gone; a U+FFFD would split every word.
    """
    return text.replace("\x00", "")


def repodoc_text(path: str, data: bytes) -> tuple[str, str]:
    """``(title, body)`` to index for a repo doc's shown bytes, free of NUL (:func:`strip_nul`).

    ``.json``: devague prose (:func:`devague_text.text_for`; title from its title/slug), or
    the raw text if it does not parse. Markdown and plain text: the raw text (title from the
    first ``# `` heading). Anything else, i.e. binary design assets: no body, so only the
    path and the file name (the title) are searchable.
    """
    name = strip_nul(posixpath.basename(path))
    low = path.lower()
    if low.endswith(".json"):
        title, body = _json_repodoc_text(path, name, data)
    elif low.endswith(_TEXT_SUFFIXES):
        # stripped before the heading is looked for, so "# \x00" is not a title
        return _text_repodoc_text(name, strip_nul(data.decode("utf-8", errors="replace")))
    else:
        return name, ""
    return strip_nul(title), strip_nul(body)


_SHOWN_SQL = """
SELECT d.id, d.removed_at, d.visibility AS doc_vis,
       dr.sha256 AS draft_sha, dr.visibility AS draft_vis,
       nv.sha256 AS newest_sha,
       COALESCE(cv.sha256, nv.sha256) AS ver_sha,
       COALESCE(cv.visibility, nv.visibility) AS ver_vis,
       q.doc_id AS queued, q.shown_sha256 AS q_sha, q.shown_visibility AS q_vis
FROM repodocs d
LEFT JOIN repodoc_drafts dr ON dr.doc_id = d.id
LEFT JOIN repodoc_versions cv ON cv.id = d.current_version_id
LEFT JOIN repodoc_versions nv ON nv.id = (
    SELECT id FROM repodoc_versions WHERE doc_id = d.id
    ORDER BY committed_at DESC, id DESC LIMIT 1)
LEFT JOIN repodoc_index_queue q ON q.doc_id = d.id
WHERE d.id = ANY(%s)
"""


def _shown(r: Any) -> tuple[str, str] | None:
    """``(sha, visibility)`` of the bytes a repo doc shows, or None when it shows nothing."""
    if r["draft_sha"] is not None and r["draft_sha"] != r["newest_sha"]:
        sha, vis = r["draft_sha"], r["draft_vis"]
    elif r["ver_sha"] is not None:
        sha, vis = r["ver_sha"], r["ver_vis"]
    else:
        return None
    return sha, ("public" if r["doc_vis"] == "public" and vis == "public" else "private")


def _plan_one(r: Any) -> tuple | None:
    """The action for one repo doc row, or None when its job is up to date."""
    if r["removed_at"] is not None:
        return ("drop", r["id"], None, None) if r["queued"] is not None else None
    shown = _shown(r)
    if shown is None:
        return None
    sha, vis = shown
    if r["queued"] is None or r["q_sha"] != sha:
        return ("enqueue", r["id"], sha, vis)
    if r["q_vis"] != vis:
        return ("visibility", r["id"], sha, vis)
    return None


def discover_repodocs(store: Store, *, now: float | None = None) -> int:
    """(Re)open the job of every changed repo doc whose shown sha256 moved; drop removed docs.

    Only docs marked in ``repodoc_dirty`` are planned. With no marks it reads one row of one
    table, read-only, and returns. Otherwise one transaction claims the marks (``DELETE ...
    RETURNING``) and then, in a later statement and so a later snapshot, reads only the
    claimed docs: a writer still holding a mark's row lock (the trigger's upsert takes one)
    makes the claim wait for its commit, and a mark committed after the claim stays for the
    next run, so no change is lost between plan and clear. A claimed doc that no longer
    exists (hard-deleted) is dropped from the index.

    Returns how many jobs were enqueued or re-opened. A visibility change without new bytes
    updates the job and the indexed row at once (no re-index). A doc whose new text is
    private has its indexed row made private immediately, before it is re-indexed (fail
    closed).
    """
    with store.reader() as conn:
        if conn.execute("SELECT 1 FROM repodoc_dirty LIMIT 1").fetchone() is None:
            return 0
    stamp = time.time() if now is None else now
    n = 0
    with store.connect() as conn:
        claimed = [
            r[0] for r in conn.execute("DELETE FROM repodoc_dirty RETURNING doc_id").fetchall()
        ]
        if not claimed:  # another discovery claimed them first
            return 0
        rows = conn.execute(_SHOWN_SQL, (claimed,)).fetchall()
        live = {r["id"] for r in rows}
        for doc_id in sorted(set(claimed) - live):  # hard-deleted: nothing left to plan
            search.drop_repodoc(conn, doc_id)
        planned = [action for action in map(_plan_one, rows) if action is not None]
        for action, doc_id, sha, vis in planned:
            if action == "drop":
                search.drop_repodoc(conn, doc_id)
                conn.execute("DELETE FROM repodoc_index_queue WHERE doc_id = %s", (doc_id,))
                continue
            if action == "enqueue":
                conn.execute(
                    "INSERT INTO repodoc_index_queue (doc_id, shown_sha256, shown_visibility,"
                    " enqueued_at) VALUES (%s, %s, %s, %s) ON CONFLICT (doc_id) DO UPDATE SET"
                    " shown_sha256 = excluded.shown_sha256,"
                    " shown_visibility = excluded.shown_visibility, state = 'pending',"
                    " attempts = 0, next_attempt_at = 0, enqueued_at = excluded.enqueued_at,"
                    " last_error = NULL",
                    (doc_id, sha, vis, stamp),
                )
                n += 1
                if vis == "private":
                    search.set_repodoc_visibility(conn, doc_id, vis)
            else:
                conn.execute(
                    "UPDATE repodoc_index_queue SET shown_visibility = %s WHERE doc_id = %s",
                    (vis, doc_id),
                )
                search.set_repodoc_visibility(conn, doc_id, vis)
    return n


class _Replaced(Exception):
    """The item's bytes changed while it was being indexed (see :func:`_still_current`)."""


def _still_current(conn: Any, item: Item) -> None:
    """Raise :class:`_Replaced` unless *item* still has the bytes it was read with.

    ``library.replace`` swaps an item's sha256 and re-opens its queue row; a job already in
    flight for the old bytes must not write their body, vectors or ``done`` over that.
    """
    row = conn.execute("SELECT sha256 FROM items WHERE id = %s", (item.id,)).fetchone()
    if row is None or row[0] != item.sha256:
        raise _Replaced(f"item {item.id} changed while indexing")


_QUEUES = (("index_queue", "item_id"), ("repodoc_index_queue", "doc_id"))


def _doc_still_current(conn: Any, doc_id: int, sha: str) -> None:
    """Raise :class:`_Replaced` unless the doc's job still targets ``sha`` and is live."""
    row = conn.execute(
        "SELECT q.shown_sha256, d.removed_at FROM repodoc_index_queue q "
        "JOIN repodocs d ON d.id = q.doc_id WHERE q.doc_id = %s",
        (doc_id,),
    ).fetchone()
    if row is None or row[0] != sha or row[1] is not None:
        raise _Replaced(f"repo doc {doc_id} changed while indexing")


class Indexer:
    """Processes the queue. ``run_once`` is synchronous; ``start`` runs it on a thread.

    Every vector it writes carries a (model id, strategy) pair (decision c42), resolved per
    job: ``model_id`` and ``strategy`` when given (both or neither); else the embedder's
    ``vector_space()`` (:meth:`culture_library.gateway.GatewayEmbedder.vector_space`, which
    reads the gateway's ``/capabilities``; when that fails the job fails and backs off, so
    no vector is written under a guessed model); else, for a plain callable that states no
    vector space (a test fake, an injected embedder), the current pair
    (:data:`~culture_library.core.embedding_strategy.CURRENT_MODEL_ID` and ``CURRENT_STRATEGY``).
    The first vector of a pair creates its HNSW index, before and outside the write
    transaction (:func:`ensure_hnsw_index_concurrently`). ``model``
    is only a display label (``/api/status``), never stored.
    """

    def __init__(
        self,
        store: Store,
        embed: search.Embedder | None = None,
        text_for: TextFor | None = None,
        *,
        now: Callable[[], float] = time.time,
        max_attempts: int = 8,
        backoff_base: float = 5.0,
        backoff_cap: float = 900.0,
        batch_size: int = 16,
        model: str = "",
        model_id: str | None = None,
        strategy: Strategy | None = None,
    ) -> None:
        if (model_id is None) != (strategy is None):
            raise ValueError("pass model_id and strategy together, or neither")
        if model_id is not None and not model_id.strip():
            raise ValueError("model_id must name the real model, e.g. google/embeddinggemma-2")
        self.store = store
        self.embed = embed
        self.text_for = text_for or default_text_for(store)
        self.now = now
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.batch_size = batch_size
        self.model = model or getattr(embed, "model", "")
        self.model_id = model_id
        self.strategy = strategy
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._swept = False  # the first run_once re-plans every repo doc (self-healing)
        self._stale_checked = False  # the first resolved pair re-opens jobs lacking it
        self._hnsw_ready: set[tuple[str, str, str]] = set()  # (table, model_id, strategy key)

    def status(self) -> dict:
        return status(self.store, now=self.now())

    def vector_space(self) -> tuple[str, Strategy]:
        """``(model_id, strategy)`` the next vectors are written under (see the class doc).

        Raises :class:`~culture_library.core.search.EmbeddingError` when the embedder cannot
        say (its ``/capabilities`` lookup failed).
        """
        if self.model_id is not None and self.strategy is not None:
            return self.model_id, self.strategy
        # the same resolution a query uses, so both select one pair (plan risk r6)
        return search.embedder_space(self.embed)

    # -- work ---------------------------------------------------------------

    def run_once(self, limit: int = 50) -> int:
        """Process due jobs, items before repo docs; return how many completed fully.

        Item jobs run first (by ``priority``, then age). Then repo docs are discovered and
        up to the rest of ``limit`` repo doc jobs run, stopping as soon as an item job is
        due again. The first run with an embedder whose pair resolves re-opens finished jobs
        that lack that pair's vectors (:func:`requeue_stale_model`).
        """
        self._requeue_stale_once()
        ids = self._due_items(limit)
        done = sum(1 for item_id in ids if self._process(item_id))
        budget = limit - len(ids)
        if budget <= 0:
            return done
        if not self._swept:
            with self.store.connect() as conn:
                mark_all_repodocs(conn)
            self._swept = True
        discover_repodocs(self.store, now=self.now())
        with self.store.reader() as conn:
            doc_ids = [
                r[0]
                for r in conn.execute(
                    "SELECT doc_id FROM repodoc_index_queue WHERE state='pending' "
                    "AND next_attempt_at <= %s ORDER BY enqueued_at, doc_id LIMIT %s",
                    (self.now(), budget),
                )
            ]
        for doc_id in doc_ids:
            if self._due_items(1):  # an item arrived: it goes ahead of the backlog
                break
            done += 1 if self._process_doc(doc_id) else 0
        return done

    def _requeue_stale_once(self) -> None:
        if self._stale_checked or self.embed is None:
            return
        try:
            model_id, strategy = self.vector_space()
        except search.EmbeddingError:
            return  # the jobs themselves fail and back off; try again next run
        requeue_stale_model(self.store, model_id, strategy)
        self._stale_checked = True

    def _due_items(self, limit: int) -> list[int]:
        with self.store.reader() as conn:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT item_id FROM index_queue WHERE state='pending' "
                    "AND next_attempt_at <= %s ORDER BY priority, enqueued_at, item_id LIMIT %s",
                    (self.now(), limit),
                )
            ]

    def _ensure_index(self, table: str, model_id: str, strategy: Strategy) -> None:
        """Build the pair's HNSW index before the job's write transaction (once per pair)."""
        pair = (table, model_id, strategy.key())
        if pair in self._hnsw_ready:
            return
        if ensure_hnsw_index_concurrently(self.store, table, model_id, strategy):
            self._hnsw_ready.add(pair)

    def _embed_chunks(self, chunks: list[str]) -> list:
        vecs: list = []
        for i in range(0, len(chunks), self.batch_size):
            vecs.extend(search._embed(self.embed, chunks[i : i + self.batch_size]))
        return vecs

    def _process_doc(self, doc_id: int) -> bool:
        """Index one repo doc's shown text (full text, then vectors); never raises."""
        try:
            with self.store.reader() as conn:
                job = conn.execute(
                    "SELECT q.shown_sha256, q.shown_visibility, d.path, d.removed_at "
                    "FROM repodoc_index_queue q JOIN repodocs d ON d.id = q.doc_id "
                    "WHERE q.doc_id = %s",
                    (doc_id,),
                ).fetchone()
            if job is None or job["removed_at"] is not None:
                return False  # discovery drops it
            sha, vis, path = job["shown_sha256"], job["shown_visibility"], job["path"]
            title, body = repodoc_text(path, self.store.blob_path(sha).read_bytes())
            path = strip_nul(path)  # a stored path cannot hold NUL; cheap, and chunks use it
            with self.store.connect() as conn:
                _doc_still_current(conn, doc_id, sha)
                search.set_repodoc_text(conn, doc_id, title, path, body, vis)
            if self.embed is not None:
                model_id, strategy = self.vector_space()
                chunks = [f"{title}\n{path}".strip()] + search.chunk_text(body)
                vecs = self._embed_chunks(chunks)
                self._ensure_index("repodoc_embeddings", model_id, strategy)
                with self.store.connect() as conn:
                    _doc_still_current(conn, doc_id, sha)
                    search.set_repodoc_embeddings(
                        conn, doc_id, chunks, vecs, model_id=model_id, strategy=strategy
                    )
            with self.store.connect() as conn:
                _doc_still_current(conn, doc_id, sha)
                conn.execute(
                    "UPDATE repodoc_index_queue SET state='done', last_error=NULL "
                    "WHERE doc_id = %s",
                    (doc_id,),
                )
                _meta_set(conn, "last_success_at", str(self.now()))
        except _Replaced:  # a newer push re-opened the job for other bytes
            return False
        except Exception as exc:  # recorded and retried; never propagates to the caller
            self._fail(doc_id, exc, table="repodoc_index_queue", key="doc_id")
            return False
        return True

    def _process(self, item_id: int) -> bool:
        item = self.store.get_item(item_id)
        try:
            if item is None:
                raise LookupError(f"item {item_id} vanished")
            self._index_body(item)
            self._index_embeddings(item)
            with self.store.connect() as conn:
                _still_current(conn, item)
                conn.execute(
                    "UPDATE index_queue SET state='done', last_error=NULL WHERE item_id = %s",
                    (item_id,),
                )
                _meta_set(conn, "last_success_at", str(self.now()))
        except _Replaced:  # not a failure: replace() re-opened the job for the new bytes
            return False
        except Exception as exc:  # recorded and retried; never propagates to the caller
            self._fail(item_id, exc)
            return False
        return True

    def _index_body(self, item: Item) -> None:
        with self.store.reader() as conn:
            done = conn.execute(
                "SELECT body_done FROM index_queue WHERE item_id = %s", (item.id,)
            ).fetchone()
        if done and done[0]:
            return
        text = self.text_for(item)
        with self.store.connect() as conn:
            _still_current(conn, item)
            search.set_body(conn, item, text)
            conn.execute("UPDATE index_queue SET body_done = true WHERE item_id = %s", (item.id,))

    def _index_embeddings(self, item: Item) -> None:
        if self.embed is None:  # full-text only: no gateway configured, not a failure
            return
        model_id, strategy = self.vector_space()
        with self.store.reader() as conn:
            body = conn.execute(
                "SELECT body FROM item_text WHERE item_id = %s", (item.id,)
            ).fetchone()
        header = f"{item.title}\n{item.author}".strip()
        chunks = [header] + search.chunk_text(body[0] if body else "")
        vecs = self._embed_chunks(chunks)
        self._ensure_index("embeddings", model_id, strategy)
        with self.store.connect() as conn:
            _still_current(conn, item)
            search.set_embeddings(conn, item.id, chunks, vecs, model_id=model_id, strategy=strategy)

    def _fail(
        self,
        item_id: int,
        exc: BaseException,
        *,
        table: str = "index_queue",
        key: str = "item_id",
    ) -> None:
        """Back off (or give up) one job; ``table``/``key`` select the repo doc queue."""
        if (table, key) not in _QUEUES:  # fixed identifiers, never caller input
            raise ValueError(f"unknown queue {table}.{key}")
        msg = f"{type(exc).__name__}: {exc}"[:500]
        with self.store.connect() as conn:
            row = conn.execute(
                f"SELECT attempts FROM {table} WHERE {key} = %s", (item_id,)  # nosec B608
            ).fetchone()
            attempts = (row[0] if row else 0) + 1
            delay = min(self.backoff_base * 2 ** (attempts - 1), self.backoff_cap)
            state = "failed" if attempts >= self.max_attempts else "pending"
            conn.execute(
                f"UPDATE {table} SET attempts=%s, state=%s, next_attempt_at=%s, "  # nosec B608
                f"last_error=%s WHERE {key} = %s",
                (attempts, state, self.now() + delay, msg, item_id),
            )
            _meta_set(conn, "last_failure_at", str(self.now()))
            _meta_set(conn, "last_error", msg)

    # -- thread -------------------------------------------------------------

    def start(self, poll: float = 2.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception:  # nosec B110 - a bad cycle must not kill the thread
                    pass
                self._wake.wait(poll)
                self._wake.clear()

        self._thread = threading.Thread(target=loop, name="culture-library-indexer", daemon=True)
        self._thread.start()

    def notify(self) -> None:
        """Wake the thread early (call after an add)."""
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
