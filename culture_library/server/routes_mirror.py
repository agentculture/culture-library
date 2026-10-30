"""Mirror worker: drain the repo-document outbox to object storage (plan task t14).

The router has no routes of its own; its lifespan starts a :class:`MirrorWorker` thread (the
same pattern as the library's ``Purger``) when a backend is available, and nothing otherwise:

- ``app.state.mirror_backend`` set before startup (tests inject a ``FakeBackend``) is used as is;
- else, when ``CULTURE_LIBRARY_S3_BUCKET`` is set, an S3 backend is built from the environment
  (``culture_library.storage.s3.from_env``). If that fails (boto3 missing, bad settings) the
  error is logged and kept on ``app.state.mirror_error`` for ``/api/status``; the server still
  starts and pushes keep queueing.

The worker drains every ``CULTURE_LIBRARY_MIRROR_INTERVAL_S`` seconds (default 5). A pass with
failures waits ``CULTURE_LIBRARY_MIRROR_BACKOFF_S`` (default 5), doubling per failing pass up to
``CULTURE_LIBRARY_MIRROR_BACKOFF_MAX_S`` (default 300), so a down backend never spins; a clean
pass resets it. Pushes never wait on the mirror: they only write outbox rows.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from culture_library.core import Store
from culture_library.core import mirror as mr

log = logging.getLogger("culture_library.server.routes_mirror")

WORKER_NAME = "culture-library-mirror"
BUCKET_ENV = "CULTURE_LIBRARY_S3_BUCKET"
INTERVAL_ENV = "CULTURE_LIBRARY_MIRROR_INTERVAL_S"
BACKOFF_ENV = "CULTURE_LIBRARY_MIRROR_BACKOFF_S"
BACKOFF_MAX_ENV = "CULTURE_LIBRARY_MIRROR_BACKOFF_MAX_S"
DEFAULT_INTERVAL_S = 5.0
DEFAULT_BACKOFF_S = 5.0
DEFAULT_BACKOFF_MAX_S = 300.0


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("ignoring %s=%r: not a number", name, raw)
        return default
    return value if value > 0 else default


class MirrorWorker(threading.Thread):
    """Drains the outbox now, then every ``interval_s``; backs off while passes fail.

    ``ran`` is set after each pass (tests wait on it). ``passes`` and ``failed_passes`` count
    passes; ``last_error`` is the last unexpected exception (``drain`` itself never raises).
    """

    def __init__(
        self,
        store: Store,
        backend: mr.MirrorBackend,
        interval_s: float = DEFAULT_INTERVAL_S,
        backoff_s: float = DEFAULT_BACKOFF_S,
        backoff_max_s: float = DEFAULT_BACKOFF_MAX_S,
    ) -> None:
        super().__init__(name=WORKER_NAME, daemon=True)
        self.store = store
        self.backend = backend
        self.interval_s = interval_s
        self.backoff_s = backoff_s
        self.backoff_max_s = max(backoff_max_s, backoff_s)
        self.ran = threading.Event()
        self.passes = 0
        self.failed_passes = 0
        self.last_error: str | None = None
        self._backoff: float | None = None
        self._stop_event = threading.Event()

    def next_delay(self, result: dict[str, int]) -> float:
        """Seconds to wait after a pass: the interval when clean, else a doubling backoff."""
        if not result.get("failed"):
            self._backoff = None
            return self.interval_s
        if self._backoff is None:
            self._backoff = self.backoff_s
        else:
            self._backoff = min(self._backoff * 2, self.backoff_max_s)
        return self._backoff

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                result = mr.drain(self.store, self.backend)
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - e.g. a locked database; retry later
                self.last_error = f"{type(exc).__name__}: {exc}"
                result = {"done": 0, "failed": 1}
            self.passes += 1
            if result.get("failed"):
                self.failed_passes += 1
            self.ran.set()
            self._stop_event.wait(self.next_delay(result))

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout)


def _backend_from_env() -> mr.MirrorBackend:
    from culture_library.storage import s3

    return s3.from_env()


def _resolve_backend(app: FastAPI) -> mr.MirrorBackend | None:
    backend = getattr(app.state, "mirror_backend", None)
    app.state.mirror_error = None
    if backend is not None or not os.environ.get(BUCKET_ENV, ""):
        return backend
    try:
        backend = _backend_from_env()
    except Exception as exc:  # noqa: BLE001 - a misconfigured mirror must not stop the server
        app.state.mirror_error = f"{type(exc).__name__}: {exc}"
        log.error("mirror disabled: %s", app.state.mirror_error)
        return None
    app.state.mirror_backend = backend
    return backend


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.mirror_worker = None
    backend = _resolve_backend(app)
    worker = None
    if backend is not None:
        store = getattr(app.state, "store", None)
        if store is None:
            store = app.state.store = Store()
        worker = app.state.mirror_worker = MirrorWorker(
            store,
            backend,
            interval_s=_float_env(INTERVAL_ENV, DEFAULT_INTERVAL_S),
            backoff_s=_float_env(BACKOFF_ENV, DEFAULT_BACKOFF_S),
            backoff_max_s=_float_env(BACKOFF_MAX_ENV, DEFAULT_BACKOFF_MAX_S),
        )
        worker.start()
    try:
        yield
    finally:
        if worker is not None:
            worker.stop()


def newest_error(store: Store) -> str | None:
    """The ``last_error`` of the most recently enqueued failing pending row, or None."""
    with store.reader() as conn:
        row = conn.execute(
            "SELECT last_error FROM mirror_outbox WHERE state = 'pending' AND last_error != ''"
            " ORDER BY enqueued_at DESC, id DESC LIMIT 1"
        ).fetchone()
    return row[0] if row else None


def status_block(store: Store, state=None, private: bool = True) -> dict:
    """``{enabled, pending, oldest_pending_age_s, last_error}`` for ``/api/status``.

    ``enabled`` means a backend is in place (injected or built at startup). ``last_error`` is
    the backend-config error if there is one, else the newest error among pending rows. A row
    error can name a private repo path, so callers without private visibility see only the
    config error (or ``None``).
    """
    stats = mr.pending_stats(store)
    config_error = getattr(state, "mirror_error", None)
    enabled = getattr(state, "mirror_backend", None) is not None and not config_error
    error = config_error or (newest_error(store) if private else None)
    return {
        "enabled": enabled,
        "pending": stats["count"],
        "oldest_pending_age_s": stats["oldest_age_seconds"],
        "last_error": error,
    }


router = APIRouter(tags=["mirror"], lifespan=_lifespan)
