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
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Callable

from . import ingest, search
from .errors import ValidationError
from .models import Item
from .store import Store, register_schema

register_schema("""
CREATE TABLE IF NOT EXISTS index_queue (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done', 'failed')),
    body_done INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    enqueued_at REAL NOT NULL,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS index_meta (
    key TEXT PRIMARY KEY,
    value TEXT
)
""")

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
    with store.connect() as conn:
        cur = conn.execute(
            "UPDATE index_queue SET state='pending', attempts=0, next_attempt_at=0 "
            "WHERE state='failed'"
        )
        return cur.rowcount


def requeue_unembedded(store: Store) -> int:
    """Re-open finished jobs that have no embeddings (indexed while no embedder was set).

    The body stays indexed (``body_done`` is kept), so only the embeddings are computed.
    """
    with store.connect() as conn:
        cur = conn.execute(
            "UPDATE index_queue SET state='pending', attempts=0, next_attempt_at=0 "
            "WHERE state='done' AND item_id NOT IN (SELECT DISTINCT item_id FROM embeddings)"
        )
        return cur.rowcount


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
        return cur.rowcount


def requeue_all(store: Store) -> int:
    """Re-run every job (e.g. after the text extractor improves)."""
    with store.connect() as conn:
        cur = conn.execute(
            "UPDATE index_queue SET state='pending', body_done=0, attempts=0, next_attempt_at=0"
        )
        return cur.rowcount


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
        """Process due jobs; return how many completed fully."""
        with self.store.reader() as conn:
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT item_id FROM index_queue WHERE state='pending' "
                    "AND next_attempt_at <= ? ORDER BY enqueued_at LIMIT ?",
                    (self.now(), limit),
                )
            ]
        return sum(1 for item_id in ids if self._process(item_id))

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

    def _fail(self, item_id: int, exc: BaseException) -> None:
        msg = f"{type(exc).__name__}: {exc}"[:500]
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT attempts FROM index_queue WHERE item_id=?", (item_id,)
            ).fetchone()
            attempts = (row[0] if row else 0) + 1
            delay = min(self.backoff_base * 2 ** (attempts - 1), self.backoff_cap)
            state = "failed" if attempts >= self.max_attempts else "pending"
            conn.execute(
                "UPDATE index_queue SET attempts=?, state=?, next_attempt_at=?, last_error=? "
                "WHERE item_id=?",
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
