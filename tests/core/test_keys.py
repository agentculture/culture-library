"""Hash-only API keys with scopes and an optional kind list (core, no HTTP)."""

from __future__ import annotations

import sqlite3

import pytest

from culture_library.core import keys
from culture_library.core.errors import NotFoundError, ValidationError


def test_mint_returns_plaintext_once_and_stores_only_a_hash(store):
    key, raw = keys.mint(store, "laptop", ["read"])
    assert raw.startswith("clk_")
    assert raw.split("_", 2)[1] == key.key_id
    with store.reader() as c:
        row = dict(c.execute("SELECT * FROM api_keys WHERE id = ?", (key.id,)).fetchone())
    assert raw not in "".join(str(v) for v in row.values())
    assert row["key_hash"] == keys.hash_key(raw)
    assert not hasattr(key, "key_hash")


def test_db_file_never_contains_plaintext(store):
    _, raw = keys.mint(store, "laptop", ["write"])
    secret = raw.split("_", 2)[2].encode()
    for path in store.data_dir.glob("library.db*"):
        assert secret not in path.read_bytes()


def test_authenticate_round_trip_and_last_used(store):
    key, raw = keys.mint(store, "phone", ["read", "write"], kinds=["diary"])
    got = keys.authenticate(store, raw)
    assert got.id == key.id
    assert got.scopes == ("read", "write")
    assert got.kinds == ("diary",)
    assert got.private is False
    assert got.last_used is not None


@pytest.mark.parametrize(
    "raw",
    ["", "clk_", "clk__x", "xtk_abcd1234_secret", "clk_deadbeef_wrongsecret", "nonsense"],
)
def test_authenticate_rejects_bad_keys(store, raw):
    keys.mint(store, "k", ["read"])
    with pytest.raises(keys.AuthError):
        keys.authenticate(store, raw)


def test_wrong_secret_for_real_key_id_is_rejected(store):
    key, raw = keys.mint(store, "k", ["read"])
    with pytest.raises(keys.AuthError):
        keys.authenticate(store, f"clk_{key.key_id}_not-the-secret")


def test_revoked_key_is_rejected(store):
    key, raw = keys.mint(store, "k", ["read"])
    revoked = keys.revoke(store, key.id)
    assert revoked.revoked_at is not None
    with pytest.raises(keys.AuthError):
        keys.authenticate(store, raw)


def test_revoke_unknown_key(store):
    with pytest.raises(NotFoundError):
        keys.revoke(store, 999)


def test_list_keys(store):
    a, _ = keys.mint(store, "a", ["read"])
    b, _ = keys.mint(store, "b", ["admin"])
    assert [k.name for k in keys.list_keys(store)] == ["a", "b"]
    assert keys.list_keys(store)[1].private is True  # admin implies private


def test_private_flag(store):
    key, _ = keys.mint(store, "reader", ["read"], private=True)
    assert key.private is True


@pytest.mark.parametrize(
    "scopes, kinds",
    [([], None), (["root"], None), (["read"], []), (["write"], ["film"])],
)
def test_mint_validates(store, scopes, kinds):
    with pytest.raises(ValidationError):
        keys.mint(store, "k", scopes, kinds=kinds)


def test_mint_requires_name(store):
    with pytest.raises(ValidationError):
        keys.mint(store, "  ", ["read"])


def test_scopes_are_normalised_and_deduped(store):
    key, _ = keys.mint(store, "k", ["write", "read", "write"])
    assert key.scopes == ("read", "write")


def test_key_ids_are_unique_column(store):
    key, _ = keys.mint(store, "k", ["read"])
    with pytest.raises(sqlite3.IntegrityError):
        with store.connect() as c:
            c.execute(
                "INSERT INTO api_keys (name, key_id, key_hash, scopes, private, created_at)"
                " VALUES ('x', ?, 'h', 'read', 0, 'now')",
                (key.key_id,),
            )
