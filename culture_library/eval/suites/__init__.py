"""The eval suite registry.

Every module in this package other than ``base`` (and ``_private`` ones) that defines a
module-level ``SUITE`` is registered automatically, like ``server.create_app`` includes every
``routes_*.py``: a new suite is one new file, no edit here. Importing this package needs no
third-party dependency; only the judged suite needs ragas, imported lazily when it scores.
"""

from __future__ import annotations

import contextlib
import importlib
import pkgutil
from typing import Iterator

from .base import HIGHER, LOWER, Metric, Suite, SuiteContext, SuiteScore, describe

_REGISTRY: dict[str, Suite] = {}


def register(suite: Suite) -> Suite:
    """Add ``suite``; names and metric keys must each belong to exactly one suite."""
    if suite.name in _REGISTRY:
        raise ValueError(f"suite {suite.name!r} is already registered")
    taken = {m.name: s.name for s in _REGISTRY.values() for m in s.metrics}
    for m in suite.metrics:
        if m.name in taken:
            raise ValueError(
                f"metric {m.name!r} of {suite.name!r} already belongs to {taken[m.name]!r}"
            )
    _REGISTRY[suite.name] = suite
    return suite


def unregister(name: str) -> None:
    _REGISTRY.pop(name, None)


@contextlib.contextmanager
def registered(suite: Suite) -> Iterator[Suite]:
    """Register ``suite`` for the duration of a ``with`` block (for tests), then undo it."""
    register(suite)
    try:
        yield suite
    finally:
        unregister(suite.name)


def get(name: str) -> Suite:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown suite {name!r}; registered: {', '.join(names())}") from None


def all_suites() -> list[Suite]:
    return sorted(_REGISTRY.values(), key=lambda s: s.name)


def names() -> list[str]:
    return [s.name for s in all_suites()]


def catalog() -> list[dict]:
    """JSON-ready catalog of every registered suite and its metrics."""
    return [describe(s) for s in all_suites()]


def _discover() -> None:
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if info.name == "base" or info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        suite = getattr(module, "SUITE", None)
        if suite is not None and suite.name not in _REGISTRY:
            register(suite)


_discover()

__all__ = [
    "HIGHER",
    "LOWER",
    "Metric",
    "Suite",
    "SuiteContext",
    "SuiteScore",
    "all_suites",
    "catalog",
    "describe",
    "get",
    "names",
    "register",
    "registered",
    "unregister",
]
