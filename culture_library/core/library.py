"""The catalog service: add (ingest + index + feed), list, get, archive, restore, purge.

Cited from agentculture/xteink @ ea2f643 (branch feat/issue-1), path xteink/core/library.py
-- cite-don't-import; modified: the store, ingest and dedup live in
:mod:`~culture_library.core.store` / :mod:`~culture_library.core.ingest`; every read is
visibility-aware (``include_private``); delete became *archive* with a 30-day restore window
and a purge job (decision c45); every change appends a :mod:`~culture_library.core.feed`
event in the same transaction; search keeps xteink's title/author substring semantics.

**Add is one transaction.** :func:`add` passes an ``on_insert`` hook through
:func:`~culture_library.core.ingest.ingest` to ``Store.add_item``; it runs inside the insert's
``BEGIN IMMEDIATE`` transaction before the commit: title and body index (the text ingest
already extracted) + index queue with the body marked done, so the background indexer only
embeds (:func:`~culture_library.core.indexer.index_on_add`), and the ``created`` feed event, and it
re-writes a missing blob (a purge that removed the bytes while a same-bytes upload was in
flight). For bytes already stored, a follow-up transaction heals a missing ``created`` event
(an item stored by another path) and restores an archived item - a pusher retrying is asking
for it back - but only for a caller who may see it: bytes matching a private item raise
:class:`ConflictError` (opaque, no id) for callers without private access, and that item is
never touched; nor is an archived item of a kind the caller may not write
(:class:`ForbiddenKindError`, the restore route's ``forbidden_kind``). One exception keeps a
write-only pusher's retries idempotent: the key that created the private item
(``uploader``, recorded in its ``created`` event and in :mod:`~culture_library.core.uploaders`)
gets it back while it is live.

**Visibility (d4).** Every read and change takes ``include_private`` and ``owner`` (the
caller's key id): an item is visible if it is public, or ``include_private``, or ``owner``
uploaded it. ``owner`` widens nothing else (kind limits are the API's ``check``).

**Archive/purge (c45).** ``archive`` stamps ``archived_at`` and appends ``archived`` (with
``purge_after``); archived items vanish from list, search, get and file (except
``include_archived`` for private-scope callers). ``restore`` clears it until
``archived_at + RETENTION`` and appends ``updated`` ``{"restored": true}``.
:func:`purge_expired` deletes items archived longer than :data:`RETENTION` (index rows
cascade; the FTS row is removed explicitly), appends ``purged``, and unlinks the blob unless
another table still names its sha256. Scheduling: :class:`Purger` runs it on a daemon thread
every ``CULTURE_LIBRARY_PURGE_INTERVAL_S`` seconds (default 3600, ``0`` disables); the
server's library router starts one in its lifespan. Running ``purge_expired`` from cron is
equivalent - it is idempotent.

Stdlib only, no network.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from . import feed, indexer, search, uploaders
from .errors import CoreError, NotFoundError, ValidationError
from .ingest import (
    DEFAULT_LIMITS,
    IngestLimits,
    _record_refusal,
    _validate,
    extract_text_ex,
    ingest,
)
from .models import KINDS, VISIBILITIES, Item
from .store import Store

RETENTION = timedelta(days=30)
PURGE_INTERVAL_ENV = "CULTURE_LIBRARY_PURGE_INTERVAL_S"
DEFAULT_PURGE_INTERVAL_S = 3600.0
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

_COLS = "id, sha256, kind, title, author, format, size, visibility, created_at, archived_at"
_SEL = f"SELECT {_COLS} FROM items"  # nosec B608 - fixed column list


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")


def _parse(stamp: str) -> datetime:
    when = datetime.fromisoformat(stamp)
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _item(row: Any) -> Item:
    return Item(**{k: row[k] for k in row.keys()})


def _row(conn: Any, item_id: int, *, lock: bool = False) -> Item | None:
    """The item, or ``None``; ``lock`` takes the row lock (``FOR UPDATE``) for a writer."""
    sql = _SEL + " WHERE id = %s" + (" FOR UPDATE" if lock else "")
    row = conn.execute(sql, (item_id,)).fetchone()
    return _item(row) if row else None


def _event(conn: Any, etype: str, item: Item, data: dict | None = None) -> None:
    feed.append(
        conn,
        {
            "type": etype,
            "item_id": item.id,
            "sha256": item.sha256,
            "kind": item.kind,
            "visibility": item.visibility,
            "data": data,
        },
    )


def default_kind(filename: str) -> str:
    """xteink's default (``book`` for an EPUB) widened: anything else is a ``document``."""
    return "book" if (filename or "").lower().endswith(".epub") else "document"


def check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise ValidationError(f"kind must be one of {', '.join(KINDS)}", "invalid_kind")
    return kind


def check_visibility(visibility: str | None) -> str | None:
    if visibility is not None and visibility not in VISIBILITIES:
        raise ValidationError(
            f"visibility must be one of {', '.join(VISIBILITIES)}", "invalid_visibility"
        )
    return visibility


# ------------------------------------------------------------------ add


class ConflictError(CoreError):
    """The bytes are already stored as an item this caller may not see (opaque: no id)."""

    code = "conflict"


CONFLICT_DETAIL = "an item with these bytes already exists"


class ForbiddenKindError(CoreError):
    """The caller may not write the existing item's kind (the API answers 403)."""

    code = "forbidden_kind"


class DuplicateContentError(CoreError):
    """The replacement bytes already belong to ANOTHER item (the API answers 409).

    Carries that item's id and visibility so the route can decide how much to reveal."""

    code = "duplicate_content"

    def __init__(self, other_id: int, other_visibility: str) -> None:
        super().__init__(f"these bytes already belong to item {other_id}")
        self.other_id = other_id
        self.other_visibility = other_visibility


def _announce(conn: Any, item: Item, uploader: str | None = None, body: str | None = None) -> None:
    """Index + ``created`` event, once per item (heals items stored by another path).

    ``uploader`` (the creating key's id) is recorded in the event's data; ``body`` (text
    already extracted by ingest) is indexed now instead of by the background indexer."""
    announced = conn.execute(
        "SELECT 1 FROM feed WHERE item_id = %s AND type = 'created' AND subject = 'item' "
        "LIMIT 1",
        (item.id,),
    ).fetchone()
    if announced is None:
        indexer.index_on_add(conn, item, body)
        _event(conn, "created", item, {"uploader": uploader} if uploader else None)


def uploader_of(store: Store, item_id: int) -> str | None:
    """The key id whose upload created ``item_id`` (``None`` when unknown)."""
    with store.reader() as conn:
        return uploaders.uploader(conn, item_id)


def uploaded_by(store: Store, item_id: int, key_id: str | None) -> bool:
    with store.reader() as conn:
        return uploaders.uploaded_by(conn, item_id, key_id)


def _own_private_retry(store: Store, item_id: int, uploader: str | None) -> Item:
    """The existing private item for the uploader's own idempotent retry, else a conflict."""
    with store.reader() as conn:
        current = _row(conn, item_id)
        own = uploaders.uploaded_by(conn, item_id, uploader)
    if current is not None and current.archived_at is None and own:
        return current
    raise ConflictError(CONFLICT_DETAIL)


def _reuse_existing(
    store: Store, item_id: int, data: bytes, writable_kinds: frozenset[str] | None
) -> Item | None:
    """Re-announce (and restore, if archived and the kind is writable) an already-stored
    item; ``None`` when it was purged in the meantime."""
    with store.connect() as conn:
        current = _row(conn, item_id)
        if current is None:
            return None
        if (
            current.archived_at is not None
            and writable_kinds is not None
            and current.kind not in writable_kinds
        ):
            raise ForbiddenKindError(f"this key cannot write kind {current.kind}")
        _ensure_blob(store, conn, current.sha256, data)
        _announce(conn, current)
        if current.archived_at is not None:
            conn.execute("UPDATE items SET archived_at = NULL WHERE id = %s", (current.id,))
            _event(conn, "updated", current, {"restored": True})
            current = _row(conn, current.id) or current
    return current


def add(
    store: Store,
    data: bytes,
    filename: str,
    *,
    kind: str | None = None,
    title: str | None = None,
    author: str = "",
    visibility: str | None = None,
    include_private: bool = True,
    uploader: str | None = None,
    writable_kinds: frozenset[str] | None = None,
    limits: IngestLimits = DEFAULT_LIMITS,
) -> tuple[Item, bool]:
    """Validate, store, index and announce an upload; return ``(item, created)``.

    A new item is indexed and announced (``created``, recording ``uploader``) inside its
    insert transaction via ``Store.add_item(on_insert=...)``. For bytes already stored,
    ``created`` is False and, for a caller without private access (``include_private=False``)
    and a private existing item: if ``uploader`` created it and it is not archived, it is
    returned (the uploader's idempotent retry); otherwise :class:`ConflictError` is raised
    and the item is left untouched (never restored). Every other caller gets the existing
    item, restored first if it was archived - but only when the caller may write the
    *existing* item's kind (``writable_kinds``, ``None`` for any kind; the same rule as the
    restore route): otherwise an archived item raises :class:`ForbiddenKindError` and stays
    archived, while a live one is returned untouched. Refusals raise
    :class:`ValidationError`.
    """
    kind = check_kind(kind or default_kind(filename))
    check_visibility(visibility)

    extracted: list[str] = []  # ingest's text, indexed in the insert (no second extraction)

    def on_insert(conn: Any, item: Item) -> None:
        _ensure_blob(store, conn, item.sha256, data)  # a racing purge may have removed the bytes
        uploaders.record(conn, item.id, uploader)
        _announce(conn, item, uploader, extracted[-1] if extracted else None)

    item, created, _text = ingest(
        store,
        data,
        filename,
        kind=kind,
        title=title,
        author=(author or "").strip(),
        visibility=visibility,
        limits=limits,
        on_insert=on_insert,
        on_text=extracted.append,
    )
    if created:
        return item, True
    if item.visibility != "public" and not include_private:
        return _own_private_retry(store, item.id, uploader), False
    current = _reuse_existing(store, item.id, data, writable_kinds)
    if current is None:  # purged between the dedup and now: store it afresh
        return add(
            store,
            data,
            filename,
            kind=kind,
            title=title,
            author=author,
            visibility=visibility,
            include_private=include_private,
            uploader=uploader,
            writable_kinds=writable_kinds,
            limits=limits,
        )
    return current, False


def replace(
    store: Store,
    item_id: int,
    data: bytes,
    filename: str,
    *,
    include_private: bool = False,
    owner: str | None = None,
    limits: IngestLimits = DEFAULT_LIMITS,
) -> Item:
    """Swap a live, visible item's bytes under the same id; return the updated item.

    Validation (the upload's :func:`~culture_library.core.ingest._validate` +
    ``extract_text_ex``) runs first, outside the write lock, so every refusal raises the
    upload's :class:`ValidationError` code and changes nothing. Then, in one transaction:
    equal bytes return the item untouched (no event; a missing blob is re-written); bytes
    owned by *another* item raise :class:`DuplicateContentError` before any UPDATE;
    otherwise the new blob is written if missing (inside the transaction, so A -> B -> A
    leaves A's blob), the row's sha256/size/format change, the old embeddings are dropped,
    the body is re-indexed and the queue row re-opened for the embedder, and an ``updated``
    event ``{replaced, old_sha256, size, format}`` is appended. After the commit the old
    blob is unlinked unless something still names it (:func:`_unlink_unreferenced`).
    Title, author, kind, visibility, created_at and the uploader are untouched. An absent,
    invisible or archived item raises :class:`NotFoundError`.
    """
    try:
        fmt = _validate(data, filename, limits)
        text = extract_text_ex(data, fmt, limits).text
    except ValidationError as err:
        _record_refusal(store, err, filename, len(data))
        raise
    sha = hashlib.sha256(data).hexdigest()
    with store.connect() as conn:
        item = _row(conn, item_id, lock=True)  # two replaces of one item run one after another
        if not _visible(conn, item, include_private, False, owner):
            raise NotFoundError(f"item {item_id} not found")
        # The blob lock before the duplicate check: an add() of these bytes takes it before
        # its insert, so the check sees that add's committed row (lock order: item row, blob,
        # feed).
        lock_blob(conn, sha)
        if item.sha256 == sha:
            _ensure_blob(store, conn, sha, data)  # same bytes repair a lost file, as add() does
            return item
        other = conn.execute(
            "SELECT id, visibility FROM items WHERE sha256 = %s AND id != %s", (sha, item_id)
        ).fetchone()
        if other is not None:
            raise DuplicateContentError(other["id"], other["visibility"])
        _ensure_blob(store, conn, sha, data)
        conn.execute(
            "UPDATE items SET sha256 = %s, size = %s, format = %s WHERE id = %s",
            (sha, len(data), fmt, item_id),
        )
        conn.execute("DELETE FROM embeddings WHERE item_id = %s", (item_id,))
        updated = _row(conn, item_id)
        search.set_body(conn, updated, text)  # type: ignore[arg-type]
        indexer.enqueue(conn, item_id)
        conn.execute(
            "UPDATE index_queue SET state='pending', attempts=0, next_attempt_at=0, "
            "body_done = true WHERE item_id = %s",
            (item_id,),
        )
        _event(
            conn,
            "updated",
            updated,  # type: ignore[arg-type]
            {"replaced": True, "old_sha256": item.sha256, "size": len(data), "format": fmt},
        )
        dropped = [] if _blob_referenced(conn, item.sha256) else [item.sha256]
    _unlink_unreferenced(store, dropped)
    return updated  # type: ignore[return-value]


# ------------------------------------------------------------------ reads


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_items(
    store: Store,
    *,
    q: str | None = None,
    kind: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    include_private: bool = False,
    owner: str | None = None,
    newest_first: bool = False,
) -> list[Item]:
    """Live (unarchived) items in id order (descending with ``newest_first``, so ``limit``
    keeps the newest matches); ``q`` matches title/author substrings."""
    vis, params = uploaders.visible_sql(include_private, owner)
    where = ["archived_at IS NULL", vis]
    params = list(params)
    if kind is not None:
        where.append("kind = %s")
        params.append(check_kind(kind))
    if q is not None and q.strip():
        pat = f"%{_like_escape(q.strip().lower())}%"
        where.append("(lower(title) LIKE %s ESCAPE '\\' OR lower(author) LIKE %s ESCAPE '\\')")
        params += [pat, pat]
    limit = max(1, min(int(limit), MAX_LIMIT))
    order = "id DESC" if newest_first else "id"
    sql = _SEL + " WHERE " + " AND ".join(where) + f" ORDER BY {order} LIMIT %s OFFSET %s"
    with store.reader() as conn:
        return [_item(r) for r in conn.execute(sql, (*params, limit, max(0, int(offset))))]


def _visible(
    conn: Any,
    item: Item | None,
    include_private: bool,
    include_archived: bool,
    owner: str | None = None,
) -> bool:
    if item is None:
        return False
    if not uploaders.is_visible(conn, item.id, item.visibility, include_private, owner):
        return False
    return item.archived_at is None or include_archived


def get(
    store: Store,
    item_id: int,
    *,
    include_private: bool = False,
    include_archived: bool = False,
    owner: str | None = None,
) -> Item:
    """The item, or :class:`NotFoundError` when absent *or not visible to this caller*."""
    with store.reader() as conn:
        item = _row(conn, item_id)
        visible = _visible(conn, item, include_private, include_archived, owner)
    if not visible:
        raise NotFoundError(f"item {item_id} not found")
    return item  # type: ignore[return-value]


# ------------------------------------------------------------------ archive / restore


def archive(
    store: Store,
    item_id: int,
    *,
    include_private: bool = False,
    owner: str | None = None,
    now: datetime | None = None,
) -> Item:
    """Archive (soft-delete) a visible item; idempotent for an already-archived one."""
    when = _now(now)
    with store.connect() as conn:
        item = _row(conn, item_id)
        if not _visible(conn, item, include_private, True, owner):
            raise NotFoundError(f"item {item_id} not found")
        if item.archived_at is not None:
            return item
        stamp = _iso(when)
        conn.execute("UPDATE items SET archived_at = %s WHERE id = %s", (stamp, item_id))
        _event(
            conn, "archived", item, {"archived_at": stamp, "purge_after": _iso(when + RETENTION)}
        )
        return _row(conn, item_id)  # type: ignore[return-value]


def restore(
    store: Store,
    item_id: int,
    *,
    include_private: bool = False,
    owner: str | None = None,
    now: datetime | None = None,
) -> Item:
    """Un-archive an item within :data:`RETENTION`; :class:`NotFoundError` otherwise."""
    when = _now(now)
    with store.connect() as conn:
        item = _row(conn, item_id)
        if (
            item is None
            or not _visible(conn, item, include_private, True, owner)
            or item.archived_at is None
            or when >= _parse(item.archived_at) + RETENTION
        ):
            raise NotFoundError(f"archived item {item_id} not found")
        conn.execute("UPDATE items SET archived_at = NULL WHERE id = %s", (item_id,))
        _event(conn, "updated", item, {"restored": True})
        return _row(conn, item_id)  # type: ignore[return-value]


# ------------------------------------------------------------------ purge


# Every table with a sha256 column that names a stored blob, other than the feed log (events
# outlive their blobs). An explicit list replaces the old catalog scan; a migration that adds
# such a table must add it here (tests/core/test_library_blobs.py checks the schema against it).
BLOB_TABLES = ("items", "repodoc_versions", "repodoc_drafts", "mirror_outbox")


def _blob_referenced(conn: Any, sha: str) -> bool:
    for name in BLOB_TABLES:
        sql = f"SELECT 1 FROM {name} WHERE sha256 = %s LIMIT 1"  # nosec B608 - fixed names
        if conn.execute(sql, (sha,)).fetchone():
            return True
    return False


def lock_blob(conn: Any, sha: str) -> None:
    """Take the transaction-scoped advisory lock that serialises one blob's file changes.

    Whoever stores a blob (``add``, ``replace``, a repo-document push) calls this before it
    checks that the file exists and re-writes it, and holds the lock until its transaction
    commits, row included. :func:`_unlink_now` takes the same lock before it re-checks the
    references. So the unlinker either waits for a writer's commit and then sees its row, or
    runs first and the writer then finds the file gone and writes it again: a blob that a
    committed row names is never left without its file.
    """
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"blob:{sha}",))


def _ensure_blob(store: Store, conn: Any, sha: str, data: bytes) -> None:
    """Under the blob's lock, write the file if it is missing."""
    lock_blob(conn, sha)
    if not store.blob_path(sha).exists():
        store.write_blob(data)


def file_changed_at(store: Store, item_ids: Iterable[int]) -> dict[int, str]:
    """``{item_id: at}`` of each item's latest in-place file replace (:func:`replace`).

    Read from the feed's ``updated`` events marked ``replaced`` (restores and other updates
    are not file changes); items never replaced are absent. ``created_at`` stays the upload
    time, so a reader that syncs by date takes the later of the two (OPDS, WebDAV).
    """
    ids = sorted({int(i) for i in item_ids})
    if not ids:
        return {}
    with store.reader() as conn:
        rows = conn.execute(
            "SELECT item_id, max(at) FROM feed WHERE type = 'updated' AND subject = 'item' "
            "AND data @> '{\"replaced\": true}'::jsonb AND item_id = ANY(%s) GROUP BY item_id",
            (ids,),
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def _unlink_unreferenced(store: Store, shas: list[str]) -> None:
    """Delete each blob no row names, *after* the change that dropped it has committed.

    The caller passes only blobs that were unreferenced at its commit, so the common case
    (bytes still named elsewhere) costs nothing. Unlinking inside that transaction would let
    other connections see the committed old row pointing at a deleted file until COMMIT.
    Here a fresh transaction takes each blob's advisory lock (:func:`lock_blob`) and
    re-checks the references: every writer that stores a blob re-writes a missing one under
    the same lock, so bytes that came back meanwhile are kept or restored.

    Best-effort: the change has already committed, so an unreachable database or an I/O
    error here is reported on stderr and leaves an orphan file, never fails the caller.
    """
    if not shas:
        return
    try:
        _unlink_now(store, shas)
    except (store._psycopg.Error, CoreError, OSError) as exc:
        sys.stderr.write(f"culture-library: could not remove unreferenced blobs {shas}: {exc}\n")


def _unlink_now(store: Store, shas: list[str]) -> None:
    with store.connect() as conn:
        for sha in shas:
            lock_blob(conn, sha)
            if not _blob_referenced(conn, sha):
                store.blob_path(sha).unlink(missing_ok=True)


def purge_expired(store: Store, now: datetime | None = None) -> list[int]:
    """Permanently remove items archived at least :data:`RETENTION` ago; return their ids.

    One transaction: delete the rows (index queue, text and embeddings cascade) and append
    ``purged`` events. Then each blob no other row names is unlinked by
    :func:`_unlink_unreferenced`, after the commit and under the blob's advisory lock;
    :func:`add` re-writes a missing blob under the same lock inside its own transaction, so a
    same-bytes upload racing a purge keeps its bytes.
    """
    cutoff = _now(now) - RETENTION
    purged: list[int] = []
    dropped: list[str] = []
    with store.connect() as conn:
        rows = conn.execute(_SEL + " WHERE archived_at IS NOT NULL ORDER BY id").fetchall()
        for row in rows:
            item = _item(row)
            if _parse(item.archived_at) > cutoff:  # type: ignore[arg-type]
                continue
            gone = conn.execute("DELETE FROM items WHERE id = %s RETURNING id", (item.id,))
            if gone.fetchone() is None:  # a concurrent purge got it first
                continue
            _event(conn, "purged", item, {"archived_at": item.archived_at})
            if not _blob_referenced(conn, item.sha256):
                dropped.append(item.sha256)
            purged.append(item.id)
    _unlink_unreferenced(store, dropped)
    return purged


def purge_interval_from_env(env: dict[str, str] | None = None) -> float:
    raw = (env if env is not None else os.environ).get(PURGE_INTERVAL_ENV, "")
    try:
        return float(raw) if raw.strip() else DEFAULT_PURGE_INTERVAL_S
    except ValueError:
        return DEFAULT_PURGE_INTERVAL_S


class Purger(threading.Thread):
    """Runs :func:`purge_expired` now and then every ``interval_s`` seconds until stopped.

    ``ran`` is set after each pass (tests wait on it). Errors are swallowed per pass so a
    locked database only delays the purge to the next tick.
    """

    def __init__(self, store: Store, interval_s: float = DEFAULT_PURGE_INTERVAL_S) -> None:
        super().__init__(name="culture-library-purger", daemon=True)
        self.store = store
        self.interval_s = interval_s
        self.ran = threading.Event()
        self.last_error: str | None = None
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                purge_expired(self.store)
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - keep the loop alive; retried next tick
                self.last_error = f"{type(exc).__name__}: {exc}"
            self.ran.set()
            self._stop_event.wait(self.interval_s)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout)


__all__ = [
    "CONFLICT_DETAIL",
    "ConflictError",
    "DEFAULT_LIMIT",
    "DuplicateContentError",
    "ForbiddenKindError",
    "IngestLimits",
    "MAX_LIMIT",
    "NotFoundError",
    "PURGE_INTERVAL_ENV",
    "Purger",
    "RETENTION",
    "ValidationError",
    "add",
    "archive",
    "check_kind",
    "check_visibility",
    "default_kind",
    "file_changed_at",
    "get",
    "list_items",
    "purge_expired",
    "purge_interval_from_env",
    "replace",
    "restore",
    "uploaded_by",
    "uploader_of",
]
