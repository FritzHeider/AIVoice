"""ID token validation (specs/04-auth-tenancy-rbac.md §5 step 3)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fake_oidc import CLIENT_ID, ISSUER, OTHER_KEY, SIGNING_KEY, id_token, jwks_of
from joserfc.jwk import OctKey

from va_api.auth.oidc_tokens import ALLOWED_ALGORITHMS, IdTokenClaims, IdTokenError, validate_id_token

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
NONCE = "nonce-4b1c9e"
JWKS = jwks_of(SIGNING_KEY)


def claims(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "idp-user-1",
        "email": "sam@acme-dental.test",
        "email_verified": True,
        "name": "Sam Rivera",
        "nonce": NONCE,
        "iat": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(minutes=5)).timestamp()),
    }
    return {**base, **overrides}


def validate(token: str, jwks: dict[str, Any] = JWKS) -> IdTokenClaims:
    return validate_id_token(token, jwks=jwks, issuer=ISSUER, client_id=CLIENT_ID, nonce=NONCE, now=NOW)


def reason_of(token: str, jwks: dict[str, Any] = JWKS) -> str:
    with pytest.raises(IdTokenError) as caught:
        validate(token, jwks)
    return caught.value.reason


def unsigned(header: dict[str, Any], payload: dict[str, Any]) -> str:
    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part(header)}.{part(payload)}."


def test_valid_token_gives_the_claims() -> None:
    assert validate(id_token(claims())) == IdTokenClaims(
        subject="idp-user-1", email="sam@acme-dental.test", email_verified=True, name="Sam Rivera"
    )


def test_name_is_optional() -> None:
    assert validate(id_token(claims(name=None))).name == ""
    assert validate(id_token(claims(name=7))).name == ""


def test_algorithms_are_asymmetric_only() -> None:
    assert "none" not in ALLOWED_ALGORITHMS
    assert not [alg for alg in ALLOWED_ALGORITHMS if alg.startswith("HS")]


def test_signature_failures() -> None:
    assert reason_of(id_token(claims(), key=OTHER_KEY)) == "signature"
    assert reason_of(id_token(claims(), kid="unknown-key")) == "signature"
    assert reason_of(unsigned({"alg": "none", "kid": "test-key"}, claims())) == "signature"
    secret = OctKey.generate_key(256, parameters={"kid": "test-key"})
    hs_jwks = {"keys": [secret.as_dict(private=True)]}
    hs_token = id_token(claims(), key=secret, alg="HS256")  # type: ignore[arg-type]
    assert reason_of(hs_token, hs_jwks) == "signature"
    assert reason_of("not-a-jwt") == "signature"


def test_malformed_jwks() -> None:
    assert reason_of(id_token(claims()), {"keys": [{"kty": "nope"}]}) == "jwks"
    assert reason_of(id_token(claims()), {"nokeys": True}) == "jwks"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"iss": "https://evil.acme-dental.test"}, "iss"),
        ({"iss": f"{ISSUER}/"}, "iss"),
        ({"iss": None}, "iss"),
        ({"aud": "another-client"}, "aud"),
        ({"aud": None}, "aud"),
        ({"aud": 5}, "aud"),
        ({"aud": [CLIENT_ID, "another-client"]}, "azp"),
        ({"aud": [CLIENT_ID, "another-client"], "azp": "another-client"}, "azp"),
        ({"exp": int((NOW - timedelta(seconds=61)).timestamp())}, "exp"),
        ({"exp": None}, "exp"),
        ({"exp": "soon"}, "exp"),
        ({"exp": True}, "exp"),
        ({"iat": int((NOW + timedelta(seconds=61)).timestamp())}, "iat"),
        ({"iat": None}, "iat"),
        ({"nonce": "another-nonce"}, "nonce"),
        ({"nonce": None}, "nonce"),
        ({"email": None}, "email"),
        ({"email": ["sam@acme-dental.test"]}, "email"),
        ({"sub": None}, "sub"),
        ({"email_verified": "yes"}, "email_verified"),
        ({"email_verified": 1}, "email_verified"),
    ],
)
def test_claim_failures(overrides: dict[str, Any], reason: str) -> None:
    assert reason_of(id_token(claims(**overrides))) == reason


def test_audience_list_with_azp_and_clock_skew_are_accepted() -> None:
    token = id_token(claims(aud=[CLIENT_ID, "another-client"], azp=CLIENT_ID))
    assert validate(token).subject == "idp-user-1"
    assert validate(id_token(claims(aud=[CLIENT_ID]))).subject == "idp-user-1"
    within_skew = claims(
        exp=int((NOW - timedelta(seconds=59)).timestamp()), iat=int((NOW + timedelta(seconds=59)).timestamp())
    )
    assert validate(id_token(within_skew)).subject == "idp-user-1"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), ("true", True), (False, False), ("false", False), (None, None)],
)
def test_email_verified_forms(value: Any, expected: bool | None) -> None:
    assert validate(id_token(claims(email_verified=value))).email_verified is expected
