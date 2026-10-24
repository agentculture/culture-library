"""Domain errors raised by the core services (transport-agnostic).

Cited from agentculture/xteink @ ea2f643 (branch feat/issue-1), path xteink/core/errors.py
— cite-don't-import; modified: dropped AuthError (keys/devices are out of scope here);
reworded for the library catalog.
"""


class CoreError(Exception):
    """Base class for core errors."""


class NotFoundError(CoreError):
    """A referenced item does not exist."""


class ValidationError(CoreError):
    """Caller supplied invalid data.

    ``code`` is a machine-readable refusal code (default ``validation_error``); a raiser may
    pass its own, e.g. ``ValidationError("too big", "too_large")``.
    """

    code = "validation_error"

    def __init__(self, message: str = "", code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code

    @property
    def detail(self) -> str:
        return str(self)
