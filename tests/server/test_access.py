"""Cloudflare Access JWT verifier + its all-or-nothing env config (stdlib only, no network).

Adapted from agentculture/xteink @ ea2f643, tests/server/test_access_verifier.py
(cite-don't-import): culture-library env names, pocketsync's ``unavailable`` reason.
"""

from __future__ import annotations

import json

import pytest

from culture_library.server.access import (
    ACCESS_ENV,
    DEFAULT_LEEWAY,
    ENV_AUD,
    ENV_TEAM_DOMAIN,
    AccessConfig,
    AccessConfigError,
    AccessVerifier,
    VerificationError,
    verify_rs256,
)

from .access_jwt import AUD, NOW, TEAM, Keypair, b64, claims, jwks, keypair, now, token


def verifier(fetches: list | None = None, keyset=None, **kw) -> AccessVerifier:
    def fetch():
        if fetches is not None:
            fetches.append(1)
        return keyset if keyset is not None else jwks()

    return AccessVerifier(TEAM, AUD, fetch_jwks=fetch, clock=now, **kw)


def reason(v: AccessVerifier, tok: str) -> str:
    with pytest.raises(VerificationError) as ei:
        v.verify(tok)
    return ei.value.reason


def test_valid_token_yields_sso_identity():
    ident = verifier().verify(token())
    assert (ident.kind, ident.identity, ident.subject) == ("sso", "alice@example.com", "user-sub-1")


def test_service_token_identity():
    ident = verifier().verify(token(claims(email=None, common_name="ci.access", sub="")))
    assert (ident.kind, ident.identity) == ("service", "ci.access")


def test_audience_may_be_a_single_string():
    assert verifier().verify(token(claims(aud=AUD))).kind == "sso"


def test_forged_signature_is_rejected():
    forger = Keypair(kid="kid-1", seed="forger")  # same kid, different key
    assert reason(verifier(), token(pair=forger)) == "bad_signature"


def test_tampered_payload_is_rejected():
    head, _, sig = token().split(".")
    evil = b64(json.dumps(claims(email="root@example.com")).encode())
    assert reason(verifier(), f"{head}.{evil}.{sig}") == "bad_signature"


def test_wrong_audience_is_rejected():
    assert reason(verifier(), token(claims(aud=["someone-else"]))) == "bad_audience"


def test_wrong_issuer_is_rejected():
    assert reason(verifier(), token(claims(iss="https://evil.example"))) == "bad_issuer"


def test_expiry_and_leeway():
    assert reason(verifier(), token(claims(exp=NOW - DEFAULT_LEEWAY))) == "expired"
    assert verifier().verify(token(claims(exp=NOW - 1))).kind == "sso"
    assert reason(verifier(leeway=0), token(claims(exp=NOW))) == "expired"
    assert reason(verifier(), token(claims(nbf=NOW + DEFAULT_LEEWAY + 1))) == "not_yet_valid"


@pytest.mark.parametrize(
    "header", [{"alg": "none", "kid": "kid-1"}, {"alg": "HS256", "kid": "kid-1"}, {"alg": "RS256"}]
)
def test_algorithm_confusion_and_missing_kid(header):
    assert reason(verifier(), token(header=header)) == "malformed"


@pytest.mark.parametrize("bad", ["", "a.b", "a.b.c.d", "!!.!!.!!", "x.y.z"])
def test_malformed_tokens(bad):
    assert reason(verifier(), bad) == "malformed"


@pytest.mark.parametrize("change", [{"exp": None}, {"email": None, "common_name": None}])
def test_missing_claims_are_malformed(change):
    assert reason(verifier(), token(claims(**change))) == "malformed"


def test_jwks_is_cached_and_unknown_kid_refetches_once_per_window():
    fetches: list = []
    v = verifier(fetches)
    v.verify(token())
    v.verify(token())
    assert len(fetches) == 1
    other = keypair("kid-2")
    assert reason(v, token(pair=other)) == "unknown_kid"
    assert reason(v, token(pair=other)) == "unknown_kid"
    assert len(fetches) == 2


def test_key_rotation_is_picked_up():
    rotated = {"value": jwks()}
    v = AccessVerifier(TEAM, AUD, fetch_jwks=lambda: rotated["value"], clock=now)
    v.verify(token())
    rotated["value"] = jwks(keypair("kid-2"))
    assert v.verify(token(pair=keypair("kid-2"))).kind == "sso"


def test_jwks_failure_is_unavailable_and_negative_cached():
    calls: list = []

    def boom():
        calls.append(1)
        raise OSError("network down")

    v = AccessVerifier(TEAM, AUD, fetch_jwks=boom, clock=now)
    assert reason(v, token()) == "unavailable"
    assert reason(v, token()) == "unavailable"
    assert len(calls) == 1


def test_non_signing_jwks_entries_are_ignored():
    jwk = keypair().jwk()
    keyset = {"keys": [{**jwk, "use": "enc"}, {**jwk, "kty": "EC"}, {**jwk, "alg": "RS512"}]}
    # no usable key at all is an infrastructure fault (pocketsync semantics), not a bad token
    assert reason(verifier(keyset=keyset), token()) == "unavailable"


def test_verify_rs256_rejects_wrong_length_signature():
    pair = keypair()
    assert verify_rs256(pair.n, pair.e, b"m", b"\x00") is False


def test_constructing_a_verifier_makes_no_fetch():
    fetches: list = []
    v = verifier(fetches)
    assert fetches == [] and v.jwks_url == f"https://{TEAM}/cdn-cgi/access/certs"


def test_pasted_url_scheme_is_dropped():
    assert AccessVerifier(f"http://{TEAM}/", AUD).jwks_url.startswith(f"https://{TEAM}/")


def test_config_from_env_all_or_nothing():
    assert ACCESS_ENV == (ENV_TEAM_DOMAIN, ENV_AUD)
    assert ENV_TEAM_DOMAIN == "CULTURE_LIBRARY_ACCESS_TEAM_DOMAIN"
    assert AccessConfig.from_env({}) is None
    assert AccessConfig.from_env({ENV_TEAM_DOMAIN: TEAM, ENV_AUD: AUD}) == AccessConfig(TEAM, AUD)
    with pytest.raises(AccessConfigError):
        AccessConfig.from_env({ENV_TEAM_DOMAIN: TEAM})
    with pytest.raises(AccessConfigError):
        AccessVerifier("", AUD)


def test_real_fetch_uses_urlopen_with_timeout(monkeypatch):
    import io

    import culture_library.server.access as access

    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        seen["url"], seen["timeout"] = request.full_url, timeout
        return Resp(json.dumps(jwks()).encode())

    monkeypatch.setattr(access.urllib.request, "urlopen", fake_urlopen)
    v = AccessVerifier(TEAM, AUD, clock=now)
    assert v.verify(token()).kind == "sso"
    assert seen == {"url": f"https://{TEAM}/cdn-cgi/access/certs", "timeout": 10}
