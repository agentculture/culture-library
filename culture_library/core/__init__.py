"""Core: storage and models. No network I/O beyond the library database; psycopg is lazy.

Modules no longer register schema at import: tables come from ``core/migrations/*.sql``,
applied by :class:`Store` on open, so this package imports nothing for side effects.
"""

from .errors import CoreError, NotFoundError, ValidationError
from .models import KINDS, VISIBILITIES, AddResult, Item
from .store import DatabaseError, MigrationError, Store

__all__ = [
    "AddResult",
    "CoreError",
    "DatabaseError",
    "Item",
    "KINDS",
    "MigrationError",
    "NotFoundError",
    "Store",
    "VISIBILITIES",
    "ValidationError",
]
