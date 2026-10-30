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
whose shown sha256 differs from the one their job holds, and (re)opens their job. The shown
text is the working draft when it is ahead of the newest version, else the current version
(or the newest one while the doc has no current). A removed doc is never enqueued, and a
doc removed after indexing is dropped from ``repodoc_fts``, ``repodoc_embeddings`` and its
job row. Text: ``.json`` goes through :func:`culture_library.core.devague_text.text_for`,
Markdown and plain text are indexed raw, and anything else (binary design assets: images,
HTML, PDF) indexes only its path and title (:func:`repodoc_text`).

Storage is additive, so the item tables and their ``items`` foreign keys are untouched:
repo doc jobs live in their own ``repodoc_index_queue`` (keyed by ``doc_id``, with the
shown sha256 and the strictest visibility of the doc and its shown text, and the same
``pending``/``done``/``failed`` state machine, attempts and backoff as ``index_queue``).
``index_queue`` gained a ``priority`` column (lower runs first, items default to
:data:`PRIORITY_ITEM`; added to old databases with ``ALTER TABLE ADD COLUMN``). Repo doc
jobs are a lower tier than any item: ``run_once`` runs due item jobs first, and stops the
repo doc backlog as soon as an item job is due, so an upload during the initial backfill is
indexed next. A gateway failure on a repo doc job backs off and retries exactly like an
item job; its full text is already written by then.
"""

from __future__ import annotations

import json
import posixpath
import sqlite3
import threading
import time
from typing import Callable

from . import devague_text, ingest, search
from .errors import ValidationError
from .models import Item
from .store import Store, register_migration, register_schema

register_schema("""
CREATE TABLE IF NOT EXISTS index_queue (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done', 'failed')),
    body_done INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    enqueued_at REAL NOT NULL,
    last_error TEXT,
    priority INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS index_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS repodoc_index_queue (
    doc_id INTEGER PRIMARY KEY REFERENCES repodocs(id) ON DELETE CASCADE,
    shown_sha256 TEXT NOT NULL,
    shown_visibility TEXT NOT NULL DEFAULT 'private',
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    enqueued_at REAL NOT NULL,
    last_error TEXT
)
""")

PRIORITY_ITEM = 0  # index_queue.priority: lower runs first; repo doc jobs run after all items


def _ensure_priority(conn: sqlite3.Connection) -> None:
    """Add ``index_queue.priority`` to a database created before it existed (additive)."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(index_queue)")}
    if have and "priority" not in have:
        conn.execute(
            f"ALTER TABLE index_queue ADD COLUMN priority INTEGER NOT NULL "
            f"DEFAULT {PRIORITY_ITEM}"
        )


register_migration(_ensure_priority)

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


def enqueue(conn: sqlite3.Connection, item_id: int, *, now: float | None = None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO index_queue (item_id, enqueued_at) VALUES (?, ?)",
        (item_id, time.time() if now is None else now),
    )


def index_on_add(conn: sqlite3.Connection, item: Item, body: str | None = None) -> None:
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
        conn.execute("UPDATE index_queue SET body_done=1 WHERE item_id=?", (item.id,))


def enqueue_missing(store: Store) -> int:
    """Queue items that have no queue row (pre-existing data, migrations)."""
    with store.connect() as conn:
        cur = conn.execute(
            "INSERT INTO index_queue (item_id, enqueued_at) "
            "SELECT id, ? FROM items WHERE id NOT IN (SELECT item_id FROM index_queue)",
            (time.time(),),
        )
        n = cur.rowcount
        for row in conn.execute(
            "SELECT * FROM items WHERE id NOT IN (SELECT rowid FROM search_fts)"
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
            "UPDATE index_queue SET state='pending', attempts=0, next_attempt_at=0 "
            "WHERE state='done' AND item_id NOT IN (SELECT DISTINCT item_id FROM embeddings)"
        )
        docs = conn.execute(
            "UPDATE repodoc_index_queue SET state='pending', attempts=0, next_attempt_at=0 "
            "WHERE state='done' AND doc_id NOT IN (SELECT DISTINCT doc_id FROM repodoc_embeddings)"
        )
        return cur.rowcount + docs.rowcount


def requeue_stale_model(store: Store, label: str) -> int:
    """Re-open finished jobs whose embeddings were made under another model/prompt label.

    Run at startup with the indexer's label so a prompt or model change re-embeds the corpus
    instead of comparing new query vectors with old document vectors.
    """
    if not label:
        return 0
    with store.connect() as conn:
        cur = conn.execute(
            "UPDATE index_queue SET state='pending', attempts=0, next_attempt_at=0 "
            "WHERE state='done' AND item_id IN "
            "(SELECT DISTINCT item_id FROM embeddings WHERE model != ?)",
            (label,),
        )
        docs = conn.execute(
            "UPDATE repodoc_index_queue SET state='pending', attempts=0, next_attempt_at=0 "
            "WHERE state='done' AND doc_id IN "
            "(SELECT DISTINCT doc_id FROM repodoc_embeddings WHERE model != ?)",
            (label,),
        )
        return cur.rowcount + docs.rowcount


def requeue_all(store: Store) -> int:
    """Re-run every job (e.g. after the text extractor improves)."""
    with store.connect() as conn:
        cur = conn.execute(
            "UPDATE index_queue SET state='pending', body_done=0, attempts=0, next_attempt_at=0"
        )
        docs = conn.execute(
            "UPDATE repodoc_index_queue SET state='pending', attempts=0, next_attempt_at=0"
        )
        return cur.rowcount + docs.rowcount


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)", (key, value))


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


def repodoc_text(path: str, data: bytes) -> tuple[str, str]:
    """``(title, body)`` to index for a repo doc's shown bytes.

    ``.json``: devague prose (:func:`devague_text.text_for`; title from its title/slug), or
    the raw text if it does not parse. Markdown and plain text: the raw text (title from the
    first ``# `` heading). Anything else, i.e. binary design assets: no body, so only the
    path and the file name (the title) are searchable.
    """
    name = posixpath.basename(path)
    low = path.lower()
    if low.endswith(".json"):
        raw = data.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except ValueError:
            return name, raw
        title = ""
        if isinstance(parsed, dict):
            for key in ("title", "slug", "plan_slug"):
                if isinstance(parsed.get(key), str) and parsed[key].strip():
                    title = parsed[key].strip()
                    break
        return title or name, devague_text.text_for(path, parsed)
    if low.endswith(_TEXT_SUFFIXES):
        raw = data.decode("utf-8", errors="replace")
        for line in raw.splitlines():
            if line.startswith("# ") and line[2:].strip():
                return line[2:].strip(), raw
        return name, raw
    return name, ""


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
"""


def _plan_discovery(conn: sqlite3.Connection) -> list[tuple]:
    """``(action, doc_id, sha, visibility)`` for every repo doc whose job is out of date."""
    actions = []
    for r in conn.execute(_SHOWN_SQL).fetchall():
        if r["removed_at"] is not None:
            if r["queued"] is not None:
                actions.append(("drop", r["id"], None, None))
            continue
        if r["draft_sha"] is not None and r["draft_sha"] != r["newest_sha"]:
            sha, vis = r["draft_sha"], r["draft_vis"]
        elif r["ver_sha"] is not None:
            sha, vis = r["ver_sha"], r["ver_vis"]
        else:
            continue
        vis = "public" if r["doc_vis"] == "public" and vis == "public" else "private"
        if r["queued"] is None or r["q_sha"] != sha:
            actions.append(("enqueue", r["id"], sha, vis))
        elif r["q_vis"] != vis:
            actions.append(("visibility", r["id"], sha, vis))
    return actions


def discover_repodocs(store: Store, *, now: float | None = None) -> int:
    """(Re)open the job of every repo doc whose shown sha256 changed; drop removed docs.

    Returns how many jobs were enqueued or re-opened. A visibility change without new bytes
    updates the job and the indexed row at once (no re-index). A doc whose new text is
    private has its indexed row made private immediately, before it is re-indexed (fail
    closed). Takes the write lock only when something changed.
    """
    with store.reader() as conn:
        if not _plan_discovery(conn):
            return 0
    stamp = time.time() if now is None else now
    n = 0
    with store.connect() as conn:
        for action, doc_id, sha, vis in _plan_discovery(conn):
            if action == "drop":
                search.drop_repodoc(conn, doc_id)
                conn.execute("DELETE FROM repodoc_index_queue WHERE doc_id = ?", (doc_id,))
                continue
            if action == "enqueue":
                conn.execute(
                    "INSERT INTO repodoc_index_queue (doc_id, shown_sha256, shown_visibility,"
                    " enqueued_at) VALUES (?, ?, ?, ?) ON CONFLICT (doc_id) DO UPDATE SET"
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
                    "UPDATE repodoc_index_queue SET shown_visibility = ? WHERE doc_id = ?",
                    (vis, doc_id),
                )
                search.set_repodoc_visibility(conn, doc_id, vis)
    return n


class _Replaced(Exception):
    """The item's bytes changed while it was being indexed (see :func:`_still_current`)."""


def _still_current(conn: sqlite3.Connection, item: Item) -> None:
    """Raise :class:`_Replaced` unless *item* still has the bytes it was read with.

    ``library.replace`` swaps an item's sha256 and re-opens its queue row; a job already in
    flight for the old bytes must not write their body, vectors or ``done`` over that.
    """
    row = conn.execute("SELECT sha256 FROM items WHERE id = ?", (item.id,)).fetchone()
    if row is None or row[0] != item.sha256:
        raise _Replaced(f"item {item.id} changed while indexing")


_QUEUES = (("index_queue", "item_id"), ("repodoc_index_queue", "doc_id"))


def _doc_still_current(conn: sqlite3.Connection, doc_id: int, sha: str) -> None:
    """Raise :class:`_Replaced` unless the doc's job still targets ``sha`` and is live."""
    row = conn.execute(
        "SELECT q.shown_sha256, d.removed_at FROM repodoc_index_queue q "
        "JOIN repodocs d ON d.id = q.doc_id WHERE q.doc_id = ?",
        (doc_id,),
    ).fetchone()
    if row is None or row[0] != sha or row[1] is not None:
        raise _Replaced(f"repo doc {doc_id} changed while indexing")


class Indexer:
    """Processes the queue. ``run_once`` is synchronous; ``start`` runs it on a thread."""

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
    ) -> None:
        self.store = store
        self.embed = embed
        self.text_for = text_for or default_text_for(store)
        self.now = now
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.batch_size = batch_size
        self.model = model or getattr(embed, "model", "")
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def status(self) -> dict:
        return status(self.store, now=self.now())

    # -- work ---------------------------------------------------------------

    def run_once(self, limit: int = 50) -> int:
        """Process due jobs, items before repo docs; return how many completed fully.

        Item jobs run first (by ``priority``, then age). Then repo docs are discovered and
        up to the rest of ``limit`` repo doc jobs run, stopping as soon as an item job is
        due again.
        """
        ids = self._due_items(limit)
        done = sum(1 for item_id in ids if self._process(item_id))
        budget = limit - len(ids)
        if budget <= 0:
            return done
        discover_repodocs(self.store, now=self.now())
        with self.store.reader() as conn:
            doc_ids = [
                r[0]
                for r in conn.execute(
                    "SELECT doc_id FROM repodoc_index_queue WHERE state='pending' "
                    "AND next_attempt_at <= ? ORDER BY enqueued_at, doc_id LIMIT ?",
                    (self.now(), budget),
                )
            ]
        for doc_id in doc_ids:
            if self._due_items(1):  # an item arrived: it goes ahead of the backlog
                break
            done += 1 if self._process_doc(doc_id) else 0
        return done

    def _due_items(self, limit: int) -> list[int]:
        with self.store.reader() as conn:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT item_id FROM index_queue WHERE state='pending' "
                    "AND next_attempt_at <= ? ORDER BY priority, enqueued_at LIMIT ?",
                    (self.now(), limit),
                )
            ]

    def _process_doc(self, doc_id: int) -> bool:
        """Index one repo doc's shown text (full text, then vectors); never raises."""
        try:
            with self.store.reader() as conn:
                job = conn.execute(
                    "SELECT q.shown_sha256, q.shown_visibility, d.path, d.removed_at "
                    "FROM repodoc_index_queue q JOIN repodocs d ON d.id = q.doc_id "
                    "WHERE q.doc_id = ?",
                    (doc_id,),
                ).fetchone()
            if job is None or job["removed_at"] is not None:
                return False  # discovery drops it
            sha, vis, path = job["shown_sha256"], job["shown_visibility"], job["path"]
            title, body = repodoc_text(path, self.store.blob_path(sha).read_bytes())
            with self.store.connect() as conn:
                _doc_still_current(conn, doc_id, sha)
                search.set_repodoc_text(conn, doc_id, title, path, body, vis)
            if self.embed is not None:
                chunks = [f"{title}\n{path}".strip()] + search.chunk_text(body)
                vecs: list = []
                for i in range(0, len(chunks), self.batch_size):
                    vecs.extend(search._embed(self.embed, chunks[i : i + self.batch_size]))
                with self.store.connect() as conn:
                    _doc_still_current(conn, doc_id, sha)
                    search.set_repodoc_embeddings(conn, doc_id, chunks, vecs, self.model)
            with self.store.connect() as conn:
                _doc_still_current(conn, doc_id, sha)
                conn.execute(
                    "UPDATE repodoc_index_queue SET state='done', last_error=NULL "
                    "WHERE doc_id=?",
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
                    "UPDATE index_queue SET state='done', last_error=NULL WHERE item_id=?",
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
                "SELECT body_done FROM index_queue WHERE item_id=?", (item.id,)
            ).fetchone()
        if done and done[0]:
            return
        text = self.text_for(item)
        with self.store.connect() as conn:
            _still_current(conn, item)
            search.set_body(conn, item, text)
            conn.execute("UPDATE index_queue SET body_done=1 WHERE item_id=?", (item.id,))

    def _index_embeddings(self, item: Item) -> None:
        if self.embed is None:  # full-text only: no gateway configured, not a failure
            return
        with self.store.reader() as conn:
            body = conn.execute("SELECT body FROM search_fts WHERE rowid=?", (item.id,)).fetchone()
        header = f"{item.title}\n{item.author}".strip()
        chunks = [header] + search.chunk_text(body[0] if body else "")
        vecs: list = []
        for i in range(0, len(chunks), self.batch_size):
            vecs.extend(search._embed(self.embed, chunks[i : i + self.batch_size]))
        with self.store.connect() as conn:
            _still_current(conn, item)
            search.set_embeddings(conn, item.id, chunks, vecs, self.model)

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
                f"SELECT attempts FROM {table} WHERE {key}=?", (item_id,)  # nosec B608
            ).fetchone()
            attempts = (row[0] if row else 0) + 1
            delay = min(self.backoff_base * 2 ** (attempts - 1), self.backoff_cap)
            state = "failed" if attempts >= self.max_attempts else "pending"
            conn.execute(
                f"UPDATE {table} SET attempts=?, state=?, next_attempt_at=?, "  # nosec B608
                f"last_error=? WHERE {key}=?",
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
