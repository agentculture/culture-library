"""Pure-stdlib core: storage and models. No network I/O, no third-party imports."""

from . import eval_store, sources  # noqa: F401 - they register their schema
from .errors import CoreError, NotFoundError, ValidationError
from .models import KINDS, VISIBILITIES, AddResult, Item
from .store import SCHEMA, Store, register_schema

__all__ = [
    "AddResult",
    "CoreError",
    "Item",
    "KINDS",
    "NotFoundError",
    "SCHEMA",
    "Store",
    "VISIBILITIES",
    "ValidationError",
    "register_schema",
]
