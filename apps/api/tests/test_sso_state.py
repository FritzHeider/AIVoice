"""The OIDC login state store, URLs, and OAuth requests (specs/04-auth-tenancy-rbac.md §5; specs/03 §11)."""

from __future__ import annotations

import base64
import hashlib
import uuid
from urllib.parse import parse_qs, urlsplit

import fakeredis
import pytest
from api_helpers import make_settings

from va_api.auth.oidc_client import authorization_url, token_request
from va_api.auth.sso_state import (
    SSO_STATE_TTL_S,
    SsoState,
    SsoStateStore,
    SsoStateUnavailableError,
    sso_state_key,
)
from va_api.auth.sso_urls import (
    callback_url,
    dashboard_home_url,
    email_domain,
    login_error_url,
    sso_start_url,
)

RECORD = SsoState(
    connection_id=uuid.UUID("0192f1d2-7b3c-7a10-9c4e-5d6f7a8b9c0d"),
    org_id=uuid.UUID("0192f1d2-7b3c-7a10-9c4e-5d6f7a8b9c0e"),
    nonce="nonce-4b1c9e",
    code_verifier="v" * 43,
    issuer="https://login.acme-dental.test/",
    token_endpoint="https://login.acme-dental.test/token",  # noqa: S106 - a URL, not a secret
    jwks_uri="https://login.acme-dental.test/jwks",
)


@pytest.fixture
def redis() -> fakeredis.FakeAsyncRedis:
    return fakeredis.FakeAsyncRedis()


async def test_put_then_take_once(redis: fakeredis.FakeAsyncRedis) -> None:
    store = SsoStateStore(redis)
    await store.put("state-1", RECORD)
    assert sso_state_key("state-1") == "sso:state:state-1"
    assert 0 < await redis.ttl("sso:state:state-1") <= SSO_STATE_TTL_S == 600
    assert await store.take("state-1") == RECORD
    assert await store.take("state-1") is None
    assert await store.take("never-stored") is None


async def test_existing_state_is_never_overwritten(redis: fakeredis.FakeAsyncRedis) -> None:
    store = SsoStateStore(redis)
    await store.put("state-1", RECORD)
    with pytest.raises(SsoStateUnavailableError):
        await store.put("state-1", RECORD)


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[1]",
        b'{"connection_id": "x"}',
        b'{"connection_id": "0192f1d2-7b3c-7a10-9c4e-5d6f7a8b9c0d"}',
    ],
)
async def test_malformed_state_reads_as_missing(redis: fakeredis.FakeAsyncRedis, raw: bytes) -> None:
    await redis.set("sso:state:state-1", raw)
    assert await SsoStateStore(redis).take("state-1") is None


async def test_non_string_fields_read_as_missing(redis: fakeredis.FakeAsyncRedis) -> None:
    raw = RECORD.to_json().replace('"nonce-4b1c9e"', "7")
    await redis.set("sso:state:state-1", raw)
    assert await SsoStateStore(redis).take("state-1") is None


async def test_redis_failures_raise() -> None:
    server = fakeredis.FakeServer()
    server.connected = False
    store = SsoStateStore(fakeredis.FakeAsyncRedis(server=server))
    with pytest.raises(SsoStateUnavailableError):
        await store.put("state-1", RECORD)
    with pytest.raises(SsoStateUnavailableError):
        await store.take("state-1")


def test_secrets_stay_out_of_repr() -> None:
    assert "nonce-4b1c9e" not in repr(RECORD)
    assert "v" * 43 not in repr(RECORD)


@pytest.mark.parametrize(
    ("email", "domain"),
    [
        ("sam@Acme-Dental.TEST", "acme-dental.test"),
        ("  sam@acme-dental.test  ", "acme-dental.test"),
        ("", None),
        ("nope", None),
        ("a@b", None),
        ("@acme-dental.test", None),
        ("a@b@acme-dental.test", None),
        ("sam@*.acme-dental.test", None),
        (f"{'s' * 320}@acme-dental.test", None),
    ],
)
def test_email_domain(email: str, domain: str | None) -> None:
    assert email_domain(email) == domain


def test_login_urls() -> None:
    settings = make_settings(
        public_api_base_url="https://api.voiceagents.test/", dashboard_base_url="https://app.voiceagents.test"
    )
    assert sso_start_url(settings, " sam+ops@acme-dental.test ") == (
        "https://api.voiceagents.test/internal/auth/sso/start?email=sam%2Bops%40acme-dental.test"
    )
    assert callback_url(settings) == "https://api.voiceagents.test/internal/auth/sso/callback"
    assert login_error_url(settings, "invalid_state") == (
        "https://app.voiceagents.test/login?sso_error=invalid_state"
    )
    assert dashboard_home_url(settings) == "https://app.voiceagents.test/"


def test_authorization_url_uses_pkce_s256() -> None:
    url = authorization_url(
        "https://login.acme-dental.test/authorize",
        client_id="voiceagents-dashboard",
        redirect_uri="https://api.voiceagents.test/internal/auth/sso/callback",
        state="state-1",
        nonce="nonce-1",
        code_verifier="v" * 43,
        login_hint="sam@acme-dental.test",
    )
    parts = urlsplit(url)
    query = {name: values[0] for name, values in parse_qs(parts.query).items()}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(b"v" * 43).digest()).rstrip(b"=").decode()
    assert parts.netloc == "login.acme-dental.test"
    assert query == {
        "response_type": "code",
        "client_id": "voiceagents-dashboard",
        "redirect_uri": "https://api.voiceagents.test/internal/auth/sso/callback",
        "scope": "openid email profile",
        "state": "state-1",
        "nonce": "nonce-1",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "login_hint": "sam@acme-dental.test",
    }


def test_token_request_uses_basic_client_auth() -> None:
    body, headers = token_request(
        client_id="voiceagents-dashboard",
        client_secret="s3cret",  # noqa: S106 - test value
        code="code-1",
        redirect_uri="https://api.voiceagents.test/internal/auth/sso/callback",
        code_verifier="v" * 43,
    )
    assert {name: values[0] for name, values in parse_qs(body).items()} == {
        "grant_type": "authorization_code",
        "code": "code-1",
        "redirect_uri": "https://api.voiceagents.test/internal/auth/sso/callback",
        "code_verifier": "v" * 43,
    }
    assert headers == {"Authorization": f"Basic {base64.b64encode(b'voiceagents-dashboard:s3cret').decode()}"}
