"""Keys with scopes + Cloudflare Access on the HTTP API (plan task t5; covers c6, h6, c44).

* A Bearer ``clk_`` key works on any bind, Access configured or not (c44, machine clients).
* A ``read`` key is refused (403) on mutating routes and on ``/api/keys``; a ``write`` key
  scoped to ``diary`` is refused on other kinds.
* With Access configured, a verified ``Cf-Access-Jwt-Assertion`` is the operator; a forged or
  wrong-aud token is a 401 even when it reaches the origin directly; without Access configured
  the header is ignored entirely.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi import APIRouter, Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store, keys  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402
from culture_library.server.access import (  # noqa: E402
    ACCESS_HEADER,
    CSRF_HEADER,
    ENV_AUD,
    ENV_TEAM_DOMAIN,
    AccessVerifier,
)
from culture_library.server.auth import Principal, check, current_principal  # noqa: E402

from .access_jwt import AUD, TEAM, Keypair, claims, jwks, now, token, wall_clock_token  # noqa: E402

probe = APIRouter()


def _as_dict(p: Principal) -> dict:
    kinds = sorted(p.kinds) if p.kinds is not None else None
    return {
        "key_id": p.key_id,
        "scopes": sorted(p.scopes),
        "kinds": kinds,
        "private": p.private,
        "via": p.via,
    }


@probe.get("/t/items")
def list_items(p: Principal = Depends(current_principal)) -> dict:
    check(p, "read")
    return _as_dict(p)


@probe.post("/t/items/{kind}")
def add_item(kind: str, p: Principal = Depends(current_principal)) -> dict:
    check(p, "write", kind)
    return _as_dict(p)


@probe.delete("/t/items/{kind}")
def delete_item(kind: str, p: Principal = Depends(current_principal)) -> dict:
    check(p, "write", kind)
    return _as_dict(p)


class Fetches:
    def __init__(self, fail: bool = False) -> None:
        self.count = 0
        self.fail = fail

    def __call__(self):
        self.count += 1
        if self.fail:
            raise OSError("jwks down")
        return jwks()


@pytest.fixture(autouse=True)
def _no_access_env(monkeypatch):
    monkeypatch.delenv(ENV_TEAM_DOMAIN, raising=False)
    monkeypatch.delenv(ENV_AUD, raising=False)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def make_client(store, access="unset") -> TestClient:
    app = server_app.create_app()
    app.include_router(probe)
    app.state.store = store
    if access != "unset":
        app.state.access = access
    return TestClient(app)


@pytest.fixture
def fetches():
    return Fetches()


@pytest.fixture
def client(store):
    """No Access configured: the loopback/LAN bind, keys only."""
    return make_client(store)


@pytest.fixture
def sso_client(store, fetches):
    return make_client(store, AccessVerifier(TEAM, AUD, fetch_jwks=fetches, clock=now))


def bearer(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


def mint(store, *scopes, **kw) -> str:
    return keys.mint(store, "test", list(scopes), **kw)[1]


# --- no / bad credentials -------------------------------------------------------------------


def test_no_credentials_is_401(client):
    r = client.get("/t/items")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header",
    ["Bearer clk_deadbeef_nope", "Bearer ", "Basic Zm9vOmJhcg==", "clk_x_y", "Bearer xtk_a_b"],
)
def test_bad_authorization_is_401(client, store, header):
    mint(store, "admin")
    assert client.get("/t/items", headers={"Authorization": header}).status_code == 401


def test_revoked_key_is_401(client, store):
    key, raw = keys.mint(store, "gone", ["read"])
    keys.revoke(store, key.id)
    assert client.get("/t/items", headers=bearer(raw)).status_code == 401


# --- scopes (acceptance 1) ------------------------------------------------------------------


def test_read_key_reads_but_cannot_mutate_or_manage_keys(client, store):
    h = bearer(mint(store, "read"))
    assert client.get("/t/items", headers=h).status_code == 200
    for r in (
        client.post("/t/items/diary", headers=h),
        client.delete("/t/items/diary", headers=h),
        client.get("/api/keys", headers=h),
        client.post("/api/keys", headers=h, json={"name": "x", "scopes": ["read"]}),
        client.delete("/api/keys/1", headers=h),
    ):
        assert r.status_code == 403, r.request.url
        assert r.json()["detail"]["code"] == "forbidden"


def test_write_key_scoped_to_diary(client, store):
    h = bearer(mint(store, "write", kinds=["diary"]))
    assert client.post("/t/items/diary", headers=h).status_code == 200
    assert client.delete("/t/items/diary", headers=h).status_code == 200
    r = client.post("/t/items/book", headers=h)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "forbidden_kind"
    assert client.get("/t/items", headers=h).status_code == 200  # write implies read
    assert client.get("/api/keys", headers=h).status_code == 403


def test_unscoped_write_key_writes_any_kind(client, store):
    h = bearer(mint(store, "write"))
    assert client.post("/t/items/book", headers=h).status_code == 200


def test_principal_shape_for_keys(client, store):
    key, raw = keys.mint(store, "phone", ["write"], kinds=["diary"])
    assert client.get("/t/items", headers=bearer(raw)).json() == {
        "key_id": key.key_id,
        "scopes": ["read", "write"],
        "kinds": ["diary"],
        "private": False,
        "via": "key",
    }


# --- private visibility ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "scopes, flag, expected",
    [(["read"], False, False), (["read"], True, True), (["write"], False, False)],
)
def test_private_flag_on_keys(client, store, scopes, flag, expected):
    h = bearer(mint(store, *scopes, private=flag))
    assert client.get("/t/items", headers=h).json()["private"] is expected


def test_admin_key_sees_private(client, store):
    assert client.get("/t/items", headers=bearer(mint(store, "admin"))).json()["private"] is True


# --- /api/keys ------------------------------------------------------------------------------


def test_admin_mints_lists_and_revokes_keys(client, store):
    h = bearer(mint(store, "admin"))
    r = client.post(
        "/api/keys", headers=h, json={"name": "phone", "scopes": ["write"], "kinds": ["diary"]}
    )
    assert r.status_code == 201
    body = r.json()
    raw = body["key"]
    assert raw.startswith("clk_")
    assert body["api_key"]["scopes"] == ["write"]
    assert body["api_key"]["kinds"] == ["diary"]
    assert client.post("/t/items/diary", headers=bearer(raw)).status_code == 200

    listing = client.get("/api/keys", headers=h).json()["keys"]
    assert [k["name"] for k in listing] == ["test", "phone"]
    assert raw not in str(listing)
    assert all("key_hash" not in k and "key" not in k for k in listing)

    new_id = body["api_key"]["id"]
    r = client.delete(f"/api/keys/{new_id}", headers=h)
    assert r.status_code == 200
    assert r.json()["api_key"]["revoked_at"]
    assert client.post("/t/items/diary", headers=bearer(raw)).status_code == 401


def test_revoke_unknown_key_is_404(client, store):
    assert client.delete("/api/keys/999", headers=bearer(mint(store, "admin"))).status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "x", "scopes": ["root"]},
        {"name": "x", "scopes": []},
        {"name": "", "scopes": ["read"]},
        {"name": "x", "scopes": ["write"], "kinds": ["film"]},
    ],
)
def test_mint_validation_is_422(client, store, payload):
    r = client.post("/api/keys", headers=bearer(mint(store, "admin")), json=payload)
    assert r.status_code == 422


def test_keys_routes_need_auth(client):
    assert client.get("/api/keys").status_code == 401


# --- Cloudflare Access (acceptance 2) -------------------------------------------------------


def sso(tok: str | None = None) -> dict:
    return {ACCESS_HEADER: tok if tok is not None else token()}


def test_valid_access_jwt_is_the_private_operator(sso_client):
    r = sso_client.get("/t/items", headers=sso())
    assert r.status_code == 200
    assert r.json() == {
        "key_id": None,
        "scopes": ["admin", "read", "write"],
        "kinds": None,
        "private": True,
        "via": "access",
    }
    assert sso_client.get("/api/keys", headers=sso()).status_code == 200


@pytest.mark.parametrize(
    "tok",
    [
        token(pair=Keypair(kid="kid-1", seed="forger")),
        token(claims(aud=["someone-else"])),
        token(claims(iss="https://evil.example")),
        "not-a-jwt",
    ],
    ids=["forged", "wrong-aud", "wrong-iss", "garbage"],
)
def test_forged_or_wrong_aud_jwt_is_401(sso_client, tok):
    r = sso_client.get("/t/items", headers=sso(tok))
    assert r.status_code == 401


def test_access_header_is_ignored_when_access_is_not_configured(store):
    """A direct origin request cannot smuggle a JWT past an unconfigured server."""
    client = make_client(store, access=None)
    assert client.get("/t/items", headers=sso(wall_clock_token())).status_code == 401


def test_access_mutation_needs_csrf_header(sso_client):
    r = sso_client.post("/t/items/diary", headers=sso())
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "csrf"
    ok = sso_client.post("/t/items/diary", headers={**sso(), CSRF_HEADER: "1"})
    assert ok.status_code == 200


def test_bearer_decides_even_with_a_valid_jwt(sso_client, store):
    r = sso_client.get("/t/items", headers={**sso(), "Authorization": "Bearer clk_bad_key"})
    assert r.status_code == 401
    h = {**sso(), **bearer(mint(store, "read"))}
    assert sso_client.get("/t/items", headers=h).json()["via"] == "key"


def test_jwks_outage_is_503(store):
    client = make_client(store, AccessVerifier(TEAM, AUD, fetch_jwks=Fetches(fail=True)))
    r = client.get("/t/items", headers=sso(wall_clock_token()))
    assert r.status_code == 503


# --- c44: machine clients use keys, no Access needed (acceptance 3) -------------------------


def test_key_works_without_access_configured(client, store):
    assert client.get("/t/items", headers=bearer(mint(store, "read"))).status_code == 200


def test_key_works_on_access_server_without_touching_jwks(sso_client, store, fetches):
    h = bearer(mint(store, "write"))
    assert sso_client.post("/t/items/book", headers=h).status_code == 200  # no CSRF header
    assert fetches.count == 0


def test_access_from_env_is_lazy_and_keys_skip_it(store, monkeypatch):
    import culture_library.server.access as access

    calls = []

    def fake_urlopen(*a, **kw):
        calls.append(a)
        raise OSError("no network in tests")

    monkeypatch.setattr(access.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv(ENV_TEAM_DOMAIN, TEAM)
    monkeypatch.setenv(ENV_AUD, AUD)
    client = make_client(store)
    assert client.get("/t/items", headers=bearer(mint(store, "read"))).status_code == 200
    assert calls == []
    assert client.get("/t/items", headers=sso(wall_clock_token())).status_code == 503
    assert len(calls) == 1


def test_partial_access_env_is_a_server_error_not_an_open_door(store, monkeypatch):
    monkeypatch.setenv(ENV_TEAM_DOMAIN, TEAM)
    client = make_client(store)
    r = client.get("/t/items", headers=sso(wall_clock_token()))
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "access_misconfigured"
    # keys still work: Access config is only consulted when no Authorization is sent
    assert client.get("/t/items", headers=bearer(mint(store, "read"))).status_code == 200


def test_store_defaults_to_data_dir_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CULTURE_LIBRARY_DATA_DIR", str(tmp_path / "env-data"))
    raw = keys.mint(Store(), "envkey", ["read"])[1]
    app = server_app.create_app()
    app.include_router(probe)
    assert TestClient(app).get("/t/items", headers=bearer(raw)).status_code == 200
