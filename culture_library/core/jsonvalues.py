"""Helpers for jsonb values read back from Postgres. Stdlib only, no network."""

from __future__ import annotations

from typing import Any


def sorted_keys(value: Any) -> Any:
    """``value`` with object keys sorted at every depth.

    jsonb orders keys by length, then bytes; the SQLite store sorted them alphabetically and
    API responses (and the contract tests) were written against that, so reads restore it.
    """
    if isinstance(value, dict):
        return {k: sorted_keys(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [sorted_keys(v) for v in value]
    return value


__all__ = ["sorted_keys"]
