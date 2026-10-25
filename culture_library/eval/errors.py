"""Errors raised by the eval package."""

from __future__ import annotations


class EvalError(Exception):
    """Base class for eval failures."""


class EvalPolicyError(EvalError):
    """A run would send non-public text to a non-local judge."""


class EvalApiError(EvalError):
    """The library API (or the judge endpoint) answered with an error or was unreachable."""

    def __init__(self, message: str, status: int | None = None, body: object = None):
        super().__init__(message)
        self.status = status
        self.body = body
