"""Hash-only API keys with scopes and an optional kind list (core, no HTTP)."""

from __future__ import annotations

import time

import psycopg
import pytest

from culture_library.core import keys
from culture_library.core.errors import NotFoundError, ValidationError


def test_mint_returns_plaintext_once_and_stores_only_a_hash(store):
    key, raw = keys.mint(store, "laptop", ["read"])
    assert raw.startswith("clk_")
    assert raw.split("_", 2)[1] == key.key_id
    with store.reader() as c:
        row = dict(c.execute("SELECT * FROM api_keys WHERE id = %s", (key.id,)).fetchone())
    assert raw not in "".join(str(v) for v in row.values())
    assert row["key_hash"] == keys.hash_key(raw)
    assert not hasattr(key, "key_hash")


def test_database_never_contains_plaintext(store):
    _, raw = keys.mint(store, "laptop", ["write"])
    secret = raw.split("_", 2)[2]
    with store.reader() as c:
        rows = c.execute("SELECT * FROM api_keys").fetchall()
    assert secret not in "".join(str(v) for r in rows for v in r)


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
    sql = (
        "INSERT INTO api_keys (name, key_id, key_hash, scopes, private, created_at)"
        " VALUES ('x', %s, 'h', 'read', false, 'now')"
    )
    with store.connect() as c:
        with pytest.raises(psycopg.errors.UniqueViolation):
            c.execute(sql, (key.key_id,))


# ------------------------------------------------------------------ authenticate is a read


def _last_used(store, key_id):
    with store.reader() as c:
        return c.execute("SELECT last_used FROM api_keys WHERE id = %s", (key_id,)).fetchone()[0]


def test_authenticate_reads_on_a_read_only_connection(store, monkeypatch):
    _key, raw = keys.mint(store, "k", ["read"])
    seen = []
    real = store.reader

    def spy():
        seen.append(1)
        return real()

    monkeypatch.setattr(store, "reader", spy)
    assert keys.authenticate(store, raw).last_used is not None
    assert seen


def test_last_used_is_stamped_at_most_once_a_minute(store):
    key, raw = keys.mint(store, "k", ["read"])
    with store.connect() as c:
        c.execute("UPDATE api_keys SET last_used = '2026-01-01T00:00:00+00:00' WHERE id = %s",
                  (key.id,))  # fmt: skip
    first = keys.authenticate(store, raw).last_used
    assert first != "2026-01-01T00:00:00+00:00"
    assert _last_used(store, key.id) == first
    recent = "9999-01-01T00:00:00+00:00"  # "just now" as far as the throttle can tell
    with store.connect() as c:
        c.execute("UPDATE api_keys SET last_used = %s WHERE id = %s", (recent, key.id))
    assert keys.authenticate(store, raw).last_used == recent
    assert _last_used(store, key.id) == recent


@pytest.mark.parametrize("lock", ["FOR UPDATE", "FOR NO KEY UPDATE"])
def test_authenticate_never_waits_on_a_writer_holding_the_key_row(store, lock):
    key, raw = keys.mint(store, "k", ["read"])
    with store.connect() as blocker:  # the writer: holds the row lock, uncommitted
        blocker.execute(f"SELECT id FROM api_keys WHERE id = %s {lock}", (key.id,))
        start = time.monotonic()
        got = keys.authenticate(store, raw)
        waited = time.monotonic() - start
        assert got.id == key.id
        assert got.last_used is None  # the stamp was skipped, not queued
        assert waited < 0.1 + 0.4, waited  # the 100 ms budget, plus connect time
        print(f"authenticate under a held row lock took {waited * 1000:.0f} ms")
    assert _last_used(store, key.id) is None


def test_authenticate_survives_a_stamp_that_times_out(store, monkeypatch):
    """A plain UPDATE (no SKIP LOCKED) would wait; lock_timeout still bounds the stamp."""
    key, raw = keys.mint(store, "k", ["read"])
    monkeypatch.setattr(keys, "LAST_USED_BUSY_MS", 50)
    with store.connect() as blocker:
        blocker.execute("UPDATE api_keys SET name = 'busy' WHERE id = %s", (key.id,))
        start = time.monotonic()
        assert keys.authenticate(store, raw).id == key.id
        assert time.monotonic() - start < 0.5


def test_authenticate_without_any_keys_is_an_auth_error(store):
    with pytest.raises(keys.AuthError):
        keys.authenticate(store, "clk_deadbeef_secret")


# ------------------------------------------------------------------ rotate (#14)


def _rules_key(store):
    """Like culture-rules' key 3: write, newspaper + transcript, private."""
    key, _ = keys.mint(
        store, "culture-rules", ["write"], kinds=["newspaper", "transcript"], private=True
    )
    return key


def test_rotate_copies_settings_and_adds_a_kind(store):
    old = _rules_key(store)
    new, raw = keys.rotate(store, old.id, add_kinds=["todo"])
    assert (new.name, new.scopes, new.private) == (old.name, old.scopes, old.private)
    assert new.kinds == ("newspaper", "transcript", "todo")
    assert new.replaces == old.id
    assert keys.authenticate(store, raw).id == new.id
    assert keys.list_keys(store)[0].revoked_at is None  # the old key stays valid


def test_rotate_overrides_scope_private_and_drops_kinds(store):
    old = _rules_key(store)
    new, _ = keys.rotate(store, old.id, scopes=["read"], private=False, drop_kinds=["transcript"])
    assert new.scopes == ("read",)
    assert new.private is False
    assert new.kinds == ("newspaper",)


def test_rotate_any_kind_key_stays_any_kind(store):
    old, _ = keys.mint(store, "ops", ["write"])
    new, _ = keys.rotate(store, old.id, add_kinds=["todo"])
    assert new.kinds is None


@pytest.mark.parametrize("revoked", [True, False])
def test_rotate_refuses_revoked_or_missing_before_minting(store, revoked):
    old = _rules_key(store)
    target = old.id
    if revoked:
        keys.revoke(store, old.id)
    else:
        target = 999
    with pytest.raises((keys.RotationError, NotFoundError)):
        keys.rotate(store, target, add_kinds=["todo"])
    assert len(keys.list_keys(store)) == 1


def test_confirm_revokes_the_old_key(store):
    old = _rules_key(store)
    new, raw = keys.rotate(store, old.id, add_kinds=["todo"])
    gone, kept = keys.confirm_rotation(store, old.id, new.id)
    assert gone.id == old.id
    assert gone.revoked_at is not None
    assert kept.id == new.id
    assert kept.revoked_at is None
    assert keys.authenticate(store, raw).id == new.id


@pytest.mark.parametrize("case", ["unknown", "revoked", "not_a_replacement", "self"])
def test_confirm_refuses_and_changes_nothing(store, case):
    old = _rules_key(store)
    new, _ = keys.rotate(store, old.id, add_kinds=["todo"])
    other, _ = keys.mint(store, "other", ["read"])
    new_id = {"unknown": 999, "revoked": new.id, "not_a_replacement": other.id, "self": old.id}[
        case
    ]
    if case == "revoked":
        keys.revoke(store, new.id)
    with pytest.raises((keys.RotationError, NotFoundError)) as info:
        keys.confirm_rotation(store, old.id, new_id)
    if isinstance(info.value, keys.RotationError):
        assert info.value.code
    assert [k.id for k in keys.list_keys(store) if k.revoked_at is None and k.id == old.id] == [
        old.id
    ]
