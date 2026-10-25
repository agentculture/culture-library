"""API keys with scopes and an optional kind list. Only sha256 hashes + a key id are stored.

Provenance: cited (cite-don't-import) from agentculture/xteink @ ea2f643 (branch feat/issue-1),
path xteink/core/keys.py. This copy is owned here. Modified: ``clk`` prefix; scopes
(``read``/``write``/``admin``), an optional kind allow-list and a ``private`` flag stored per
key; its own ``api_keys`` table, created idempotently by each function here on first use
(xteink kept the table in store.py; :func:`register_schema` is deliberately not used, because
tests/core/test_store.py pins a bare Store to having no ``api_keys`` table); the
:class:`ApiKey` record and :class:`AuthError` live here (models.py and errors.py stay
key-free); module functions instead of a ``KeyService`` class;
:func:`revoke` returns the revoked key; :func:`authenticate` is a read (it never takes the
write lock: a missing table means no key matches) and stamps ``last_used`` best-effort, at
most once a minute per key.

Raw key format: ``clk_<key_id>_<secret>`` where ``key_id`` is 8 hex characters (the public,
loggable handle) and ``secret`` is 32 random bytes, url-safe base64. The raw key is returned
exactly once, by :func:`mint`; the database keeps ``sha256(raw)`` only.

Scope semantics (enforced by ``culture_library.server.auth.check``):

* ``read``  - read the catalog.
* ``write`` - add/change/delete items; implies ``read``. With ``kinds`` set, writes are
  limited to those item kinds.
* ``admin`` - everything, including ``/api/keys``; implies ``read`` and ``write``.

``private`` (may see private-visibility items) is true for admin keys and for keys minted
with ``private=True``.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from .errors import CoreError, NotFoundError, ValidationError
from .models import KINDS
from .store import Store, now

PREFIX = "clk"
SCOPES = ("read", "write", "admin")
LAST_USED_INTERVAL_S = 60
LAST_USED_BUSY_MS = 100
_INVALID_KEY = "invalid key"
_BY_ID = "SELECT * FROM api_keys WHERE id = ?"

_DDL = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    key_id TEXT NOT NULL UNIQUE,
    key_hash TEXT NOT NULL,
    scopes TEXT NOT NULL,
    kinds TEXT,
    private INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    last_used TEXT,
    replaces INTEGER REFERENCES api_keys(id)
);
"""


def _ensure(c: sqlite3.Connection) -> None:
    """Create ``api_keys`` if needed and add ``replaces`` to a table that predates it."""
    c.execute(_DDL)
    cols = {row[1] for row in c.execute("PRAGMA table_info(api_keys)")}
    if "replaces" not in cols:
        c.execute("ALTER TABLE api_keys ADD COLUMN replaces INTEGER REFERENCES api_keys(id)")


class AuthError(CoreError):
    """A key is malformed, unknown, revoked, or does not match its hash."""


class RotationError(ValidationError):
    """A rotate or confirm was refused; ``code`` says why. Nothing was changed."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ApiKey:
    """A stored key, without its hash. ``kinds`` is ``None`` for "any kind"."""

    id: int
    name: str
    key_id: str
    scopes: tuple[str, ...]
    kinds: tuple[str, ...] | None
    private: bool
    created_at: str
    revoked_at: str | None = None
    last_used: str | None = None
    replaces: int | None = None

    def effective_scopes(self) -> frozenset[str]:
        """The scopes with the hierarchy applied (admin > write > read)."""
        out = set(self.scopes)
        if "admin" in out:
            out |= {"write", "read"}
        if "write" in out:
            out.add("read")
        return frozenset(out)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "key_id": self.key_id,
            "scopes": list(self.scopes),
            "kinds": list(self.kinds) if self.kinds is not None else None,
            "private": self.private,
            "created_at": self.created_at,
            "revoked_at": self.revoked_at,
            "last_used": self.last_used,
            "replaces": self.replaces,
        }


def generate_key(prefix: str = PREFIX) -> tuple[str, str, str]:
    """Return ``(raw_key, key_id, key_hash)``."""
    key_id = secrets.token_hex(4)
    raw = f"{prefix}_{key_id}_{secrets.token_urlsafe(32)}"
    return raw, key_id, hash_key(raw)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def parse_key(raw: str, prefix: str = PREFIX) -> str:
    """Return the key_id from a raw key or raise :class:`AuthError`."""
    parts = raw.split("_", 2) if isinstance(raw, str) else []
    if len(parts) != 3 or parts[0] != prefix or not parts[1] or not parts[2]:
        raise AuthError(_INVALID_KEY)
    return parts[1]


def verify(raw: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_key(raw), stored_hash)


def _ordered(values: Iterable[str], allowed: tuple[str, ...], what: str) -> tuple[str, ...]:
    got = set(values)
    unknown = sorted(got - set(allowed))
    if unknown:
        raise ValidationError(f"unknown {what}: {', '.join(unknown)}; use {', '.join(allowed)}")
    return tuple(v for v in allowed if v in got)


def _row(r) -> ApiKey:
    return ApiKey(
        id=r["id"],
        name=r["name"],
        key_id=r["key_id"],
        scopes=tuple(s for s in r["scopes"].split(",") if s),
        kinds=tuple(k for k in r["kinds"].split(",") if k) if r["kinds"] is not None else None,
        private=bool(r["private"]) or "admin" in r["scopes"].split(","),
        created_at=r["created_at"],
        revoked_at=r["revoked_at"],
        last_used=r["last_used"],
        replaces=r["replaces"] if "replaces" in r.keys() else None,
    )


def mint(
    store: Store,
    name: str,
    scopes: Iterable[str],
    *,
    kinds: Iterable[str] | None = None,
    private: bool = False,
) -> tuple[ApiKey, str]:
    """Create a key; return ``(ApiKey, raw_key)``. The raw key is never retrievable again."""
    with store.connect() as c:
        return _mint(c, name, scopes, kinds=kinds, private=private)


def _mint(
    c: sqlite3.Connection,
    name: str,
    scopes: Iterable[str],
    *,
    kinds: Iterable[str] | None = None,
    private: bool = False,
    replaces: int | None = None,
) -> tuple[ApiKey, str]:
    """:func:`mint` inside the caller's transaction, optionally recording ``replaces``."""
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("key name is required")
    scope_t = _ordered(scopes, SCOPES, "scope")
    if not scope_t:
        raise ValidationError(f"a key needs at least one scope: {', '.join(SCOPES)}")
    kinds_t = None
    if kinds is not None:
        kinds_t = _ordered(kinds, KINDS, "kind")
        if not kinds_t:
            raise ValidationError("kinds must name at least one kind (omit it for any kind)")
    raw, key_id, key_hash = generate_key()
    _ensure(c)
    cur = c.execute(
        "INSERT INTO api_keys"
        " (name, key_id, key_hash, scopes, kinds, private, created_at, replaces)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            name.strip(),
            key_id,
            key_hash,
            ",".join(scope_t),
            ",".join(kinds_t) if kinds_t is not None else None,
            int(bool(private)),
            now(),
            replaces,
        ),
    )
    row = c.execute(_BY_ID, (cur.lastrowid,)).fetchone()
    return _row(row), raw


def _stamp_last_used(store: Store, row_id: int, stamp: str, cutoff: str) -> bool:
    """Best-effort ``last_used = stamp`` unless it was stamped since ``cutoff``.

    A separate, short autocommit write that waits at most :data:`LAST_USED_BUSY_MS` for the
    write lock and gives up (returns False) if the database is busy: authentication never
    waits on, or fails because of, a writer. The ``WHERE`` keeps it at most once per
    :data:`LAST_USED_INTERVAL_S` per key even across processes.
    """
    try:
        conn = sqlite3.connect(
            store.db_path, timeout=LAST_USED_BUSY_MS / 1000, isolation_level=None
        )
        try:
            conn.execute(f"PRAGMA busy_timeout = {LAST_USED_BUSY_MS}")
            cur = conn.execute(
                "UPDATE api_keys SET last_used = ?"
                " WHERE id = ? AND (last_used IS NULL OR last_used < ?)",
                (stamp, row_id, cutoff),
            )
            return cur.rowcount == 1
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def authenticate(store: Store, raw: str) -> ApiKey:
    """Return the live key for ``raw`` or raise :class:`AuthError`.

    The key is looked up on a reader connection (no write lock). ``last_used`` is then
    stamped best-effort, at most once per :data:`LAST_USED_INTERVAL_S` per key, by
    :func:`_stamp_last_used`; a busy database skips the stamp, never the authentication.
    """
    key_id = parse_key(raw)
    try:
        with store.reader() as c:
            row = c.execute("SELECT * FROM api_keys WHERE key_id = ?", (key_id,)).fetchone()
    except sqlite3.OperationalError as exc:  # no api_keys table yet: no key can match
        if "no such table" in str(exc):
            raise AuthError(_INVALID_KEY) from None
        raise
    if row is None or row["revoked_at"] or not verify(raw, row["key_hash"]):
        raise AuthError(_INVALID_KEY)
    key = _row(row)
    when = datetime.now(UTC)
    cutoff = (when - timedelta(seconds=LAST_USED_INTERVAL_S)).isoformat(timespec="seconds")
    if key.last_used is None or key.last_used < cutoff:
        stamp = when.isoformat(timespec="seconds")
        if _stamp_last_used(store, key.id, stamp, cutoff):
            key = replace(key, last_used=stamp)
    return key


def revoke(store: Store, api_key_id: int) -> ApiKey:
    """Revoke a key by row id (idempotent for an already-revoked key)."""
    with store.connect() as c:
        _ensure(c)
        c.execute(
            "UPDATE api_keys SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?",
            (now(), api_key_id),
        )
        row = c.execute(_BY_ID, (api_key_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"api key {api_key_id} not found")
    return _row(row)


def _live(c: sqlite3.Connection, api_key_id: int, what: str) -> ApiKey:
    row = c.execute(_BY_ID, (api_key_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"{what} key {api_key_id} not found")
    key = _row(row)
    if key.revoked_at is not None:
        raise RotationError(f"{what} key {api_key_id} is revoked", "key_revoked")
    return key


def rotate(
    store: Store,
    api_key_id: int,
    *,
    add_kinds: Iterable[str] = (),
    drop_kinds: Iterable[str] = (),
    scopes: Iterable[str] | None = None,
    private: bool | None = None,
) -> tuple[ApiKey, str]:
    """Mint a replacement for a live key; return ``(new ApiKey, raw_key)``.

    The new key copies the old one's name, scopes, kinds and private flag, with the given
    changes: ``add_kinds``/``drop_kinds`` edit a kind list (a key for any kind stays any
    kind), ``scopes`` and ``private`` replace the old values. It records ``replaces``. The
    old key stays valid until :func:`confirm_rotation`. A missing key raises
    :class:`NotFoundError`, a revoked one :class:`RotationError` (``key_revoked``), before
    anything is minted.
    """
    with store.connect() as c:
        _ensure(c)
        old = _live(c, api_key_id, "old")
        kinds = old.kinds
        if kinds is not None:
            drop = set(_ordered(drop_kinds, KINDS, "kind"))
            kinds = tuple(k for k in (*kinds, *_ordered(add_kinds, KINDS, "kind")) if k not in drop)
        else:
            _ordered((*add_kinds, *drop_kinds), KINDS, "kind")  # still reject unknown kinds
        return _mint(
            c,
            old.name,
            old.scopes if scopes is None else scopes,
            kinds=kinds,
            private=old.private if private is None else private,
            replaces=old.id,
        )


def confirm_rotation(store: Store, old_id: int, new_id: int) -> tuple[ApiKey, ApiKey]:
    """Revoke ``old_id`` once its replacement ``new_id`` is in use; return ``(old, new)``.

    Refuses, changing nothing, unless both keys exist and are live and ``new_id`` was minted
    by :func:`rotate` as ``old_id``'s replacement (:class:`RotationError` codes
    ``key_revoked`` and ``not_a_replacement``; a missing key is :class:`NotFoundError`).
    """
    with store.connect() as c:
        _ensure(c)
        old = _live(c, old_id, "old")
        new = _live(c, new_id, "new")
        if new.replaces != old.id:
            raise RotationError(
                f"key {new_id} was not minted as key {old_id}'s replacement", "not_a_replacement"
            )
        c.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ?", (now(), old.id))
        row = c.execute(_BY_ID, (old.id,)).fetchone()
    return _row(row), new


def list_keys(store: Store) -> list[ApiKey]:
    with store.connect() as c:
        _ensure(c)
        return [_row(r) for r in c.execute("SELECT * FROM api_keys ORDER BY id")]


__all__ = [
    "PREFIX",
    "SCOPES",
    "ApiKey",
    "AuthError",
    "RotationError",
    "authenticate",
    "confirm_rotation",
    "generate_key",
    "hash_key",
    "list_keys",
    "mint",
    "parse_key",
    "revoke",
    "rotate",
    "verify",
]
