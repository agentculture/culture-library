"""Mirror worker lifespan and the /api/status ``mirror`` block (plan task t14)."""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store  # noqa: E402
from culture_library.core.mirror import FakeBackend  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402
from culture_library.server import routes_mirror  # noqa: E402
from culture_library.server.auth import Principal, current_principal  # noqa: E402

A = "a" * 40
REMOTE = "https://github.com/agentculture/devague"


def _principal(private: bool = True) -> Principal:
    return Principal(key_id="t", scopes=frozenset({"read", "write"}), private=private)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("CULTURE_LIBRARY_S3_BUCKET", raising=False)
    monkeypatch.setenv("CULTURE_LIBRARY_PURGE_INTERVAL_S", "0")
    monkeypatch.setenv("CULTURE_LIBRARY_MIRROR_INTERVAL_S", "0.05")
    monkeypatch.setenv("CULTURE_LIBRARY_MIRROR_BACKOFF_S", "0.2")
    monkeypatch.setenv("CULTURE_LIBRARY_MIRROR_BACKOFF_MAX_S", "0.4")


def _app(tmp_path, backend=None, private=True):
    app = server_app.create_app()
    app.state.store = Store(tmp_path / "data")
    if backend is not None:
        app.state.mirror_backend = backend
    app.dependency_overrides[current_principal] = lambda: _principal(private)
    return app


def _push(client, path="docs/specs/x.md", content="hello\n"):
    return client.post(
        "/api/repodocs",
        json=dict(
            remote=REMOTE,
            path=path,
            commit=A,
            committed_at="2026-10-01T00:00:00Z",
            default_branch_head=A,
            content=content,
        ),
    )


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _mirror(client):
    return client.get("/api/status").json()["mirror"]


def test_no_backend_configured_starts_no_worker(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as c:
        assert getattr(app.state, "mirror_worker", None) is None
        assert _push(c).status_code in (200, 201)
        m = _mirror(c)
        assert m == {
            "enabled": False,
            "pending": 1,
            "parked": 0,
            "oldest_pending_age_s": m["oldest_pending_age_s"],
            "last_error": None,
        }
        assert m["oldest_pending_age_s"] >= 0
    assert not any(t.name == routes_mirror.WORKER_NAME for t in threading.enumerate())


def test_worker_drains_pushes_to_backend(tmp_path):
    be = FakeBackend()
    app = _app(tmp_path, be)
    with TestClient(app) as c:
        worker = app.state.mirror_worker
        assert worker is not None
        assert worker.is_alive()
        assert _push(c).status_code in (200, 201)
        assert _wait(lambda: _mirror(c)["pending"] == 0)
        assert any(k.endswith("docs/specs/x.md@" + A) for k in be.objects)
        m = _mirror(c)
        assert m["enabled"] is True
        assert m["oldest_pending_age_s"] is None
        assert m["last_error"] is None
    assert not worker.is_alive()


def test_failing_backend_keeps_pushes_2xx_then_recovers(tmp_path):
    be = FakeBackend()
    be.fail = ConnectionError("s3 down")
    app = _app(tmp_path, be)
    with TestClient(app) as c:
        worker = app.state.mirror_worker
        assert _push(c, "docs/specs/a.md").status_code in (200, 201)
        assert _push(c, "docs/specs/b.md").status_code in (200, 201)
        assert _wait(lambda: (_mirror(c)["last_error"] or "").startswith("ConnectionError"))
        m = _mirror(c)
        assert m["enabled"] is True
        assert m["pending"] == 2
        assert m["oldest_pending_age_s"] >= 0
        # Backoff, not a spin: over ~0.6 s at backoff 0.2-0.4 s only a few passes run.
        time.sleep(0.6)
        assert worker.failed_passes <= 6
        be.fail = None
        assert _wait(lambda: _mirror(c)["pending"] == 0)
        assert _mirror(c)["last_error"] is None
        assert len(be.objects) == 2


def test_status_keeps_existing_fields(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        body = c.get("/api/status").json()
    for key in ("private_included", "counts", "index", "last_push", "feed", "retrieval", "mirror"):
        assert key in body


def test_last_error_hidden_from_public_callers(tmp_path):
    be = FakeBackend()
    be.fail = ConnectionError("s3 down at docs/specs/x.md")
    app = _app(tmp_path, be)
    with TestClient(app) as c:
        _push(c)
        assert _wait(lambda: _mirror(c)["last_error"] is not None)
        app.dependency_overrides[current_principal] = lambda: _principal(private=False)
        m = _mirror(c)
        assert m["pending"] == 1
        assert m["last_error"] is None


def test_bucket_set_without_boto3_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("CULTURE_LIBRARY_S3_BUCKET", "lib-mirror")

    def boom():
        raise ImportError("the S3 mirror backend needs boto3: install culture-library[s3]")

    monkeypatch.setattr(routes_mirror, "_backend_from_env", boom)
    app = _app(tmp_path)
    with TestClient(app) as c:
        assert getattr(app.state, "mirror_worker", None) is None
        assert _push(c).status_code in (200, 201)
        m = _mirror(c)
        assert m["enabled"] is False
        assert "boto3" in m["last_error"]


def test_bucket_set_builds_s3_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("CULTURE_LIBRARY_S3_BUCKET", "lib-mirror")
    be = FakeBackend()
    monkeypatch.setattr(routes_mirror, "_backend_from_env", lambda: be)
    app = _app(tmp_path)
    with TestClient(app) as c:
        assert app.state.mirror_backend is be
        _push(c)
        assert _wait(lambda: _mirror(c)["pending"] == 0)


def test_worker_backoff_grows_and_resets(tmp_path):
    store = Store(tmp_path / "data")
    be = FakeBackend()
    w = routes_mirror.MirrorWorker(store, be, interval_s=1.0, backoff_s=0.5, backoff_max_s=2.0)
    assert w.next_delay({"done": 0, "failed": 1}) == 0.5
    assert w.next_delay({"done": 0, "failed": 1}) == 1.0
    assert w.next_delay({"done": 0, "failed": 1}) == 2.0
    assert w.next_delay({"done": 0, "failed": 1}) == 2.0
    assert w.next_delay({"done": 1, "failed": 0}) == 1.0
    assert w.next_delay({"done": 0, "failed": 1}) == 0.5


def test_worker_backs_off_only_without_progress_and_with_retryable_rows(tmp_path):
    """Review finding 8: parked rows, or a pass that uploaded something, keep the interval."""
    w = routes_mirror.MirrorWorker(
        Store(tmp_path / "data"), FakeBackend(), interval_s=1.0, backoff_s=0.5, backoff_max_s=2.0
    )
    assert w.next_delay({"done": 0, "failed": 1, "parked": 0, "retry": 1}) == 0.5
    assert w.next_delay({"done": 0, "failed": 1, "parked": 0, "retry": 1}) == 1.0
    assert w.next_delay({"done": 3, "failed": 1, "parked": 0, "retry": 1}) == 1.0  # progress
    assert w.next_delay({"done": 0, "failed": 1, "parked": 0, "retry": 1}) == 0.5  # reset
    assert w.next_delay({"done": 0, "failed": 1, "parked": 1, "retry": 0}) == 1.0  # all parked


def test_status_counts_parked_rows(tmp_path):
    from culture_library.core import mirror as mr

    app = _app(tmp_path)
    with TestClient(app) as c:
        store = app.state.store
        with store.connect() as conn:
            mr.enqueue(conn, "r/gone.md@working", "0" * 64)
        for _ in range(mr.MAX_ATTEMPTS):
            mr.drain(store, FakeBackend())
        m = _mirror(c)
        assert m["pending"] == 0
        assert m["parked"] == 1
        assert "FileNotFoundError" in m["last_error"]


def test_worker_stops_cleanly(tmp_path):
    store = Store(tmp_path / "data")
    w = routes_mirror.MirrorWorker(store, FakeBackend(), interval_s=60.0)
    w.start()
    assert w.ran.wait(2.0)
    w.stop()
    assert not w.is_alive()
