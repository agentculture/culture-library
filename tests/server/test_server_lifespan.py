"""App lifespan: the search indexer and the purger share the app's store."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from culture_library.server import ServerConfig  # noqa: E402
from culture_library.server import routes_search  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402


def test_lifespan_starts_indexer_on_shared_store(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CULTURE_LIBRARY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CULTURE_LIBRARY_PURGE_INTERVAL_S", "0")
    started: list = []
    queued: list = []

    class FakeIndexer:
        def __init__(self, store) -> None:
            self.store = store
            self.notified = 0

        def notify(self) -> None:
            self.notified += 1

    def fake_start(store, embedder=None):
        ix = FakeIndexer(store)
        started.append(ix)
        routes_search._indexers.append(ix)
        return ix

    monkeypatch.setattr(routes_search, "start_indexer", fake_start)
    monkeypatch.setattr(routes_search, "enqueue_missing", lambda s: queued.append(s) or 0)
    monkeypatch.setattr(routes_search, "stop_indexer", lambda: routes_search._indexers.clear())
    app = server_app.create_app(ServerConfig())
    with TestClient(app):
        assert len(started) == 1
        assert started[0].store is app.state.store
        assert queued == [app.state.store]
        routes_search.notify_indexer(app.state.store)
        assert started[0].notified == 2
    assert routes_search._indexers == []


def test_notify_indexer_without_running_indexer_is_noop(tmp_path) -> None:
    from culture_library.core import Store

    routes_search.notify_indexer(Store(tmp_path))
