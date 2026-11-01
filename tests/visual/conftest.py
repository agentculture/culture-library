"""Opt-in gate and shared fixtures for the visual parity tests (plan task t16).

These tests drive a real Chromium through Playwright, so they are off by default: the plain
``uv run pytest`` skips them and never starts a browser. Turn them on with ``-m visual`` or
``CULTURE_LIBRARY_VISUAL=1``, after ``uv sync --group visual`` and
``uv run playwright install chromium``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from .harness import Site, start_site

_HERE = Path(__file__).resolve().parent


def enabled(config) -> bool:
    mark = (config.getoption("markexpr", default="") or "").replace("not visual", "")
    return os.environ.get("CULTURE_LIBRARY_VISUAL") == "1" or "visual" in mark


def pytest_collection_modifyitems(config, items) -> None:
    if enabled(config):
        return
    skip = pytest.mark.skip(reason="visual tests are opt-in: pytest -m visual (see tests/visual)")
    for item in items:
        if _HERE in Path(str(item.fspath)).parents:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _pg_isolation(pg_dsn):
    """Visual tests share one session-scoped, seeded site; the root conftest's per-test
    truncate would empty it before the second view, so it is switched off here."""
    yield


@pytest.fixture(scope="session")
def site(pg_dsn) -> Site:
    pytest.importorskip("playwright")
    pytest.importorskip("PIL")
    with tempfile.TemporaryDirectory(prefix="culture-library-visual-") as tmp:
        handle = start_site(Path(tmp))
        try:
            yield handle
        finally:
            handle.stop()


@pytest.fixture(scope="session")
def out_dir() -> Path:
    """Where failing views leave their actual and diff images (kept after the run)."""
    raw = os.environ.get("CULTURE_LIBRARY_VISUAL_OUT")
    path = Path(raw) if raw else Path(tempfile.mkdtemp(prefix="culture-library-visual-out-"))
    path.mkdir(parents=True, exist_ok=True)
    return path
