"""Media WebSocket handshake authentication (specs/06 §4.2, specs/28 §6.2, specs/07 §3 Decision P05-T07a)."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from media_helpers import PUBLIC_HOST, TELNYX_PATH, MediaKeys, stream_query

from va_common.stream_tokens import mint_stream_token
from va_media.auth import MediaAuth, MediaAuthError, MediaClaims, signed_stream_url

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def auth_for(keys: MediaKeys, *, previous: bytes | None = None, scheme: str = "ws") -> MediaAuth:
    secrets_ = (keys.secret,) if previous is None else (keys.secret, previous)
    return MediaAuth(scheme=scheme, host=PUBLIC_HOST, public_key=keys.signing.verify_key, secrets=secrets_)


def signed(keys: MediaKeys, query: str, *, now: datetime = NOW, host: str = PUBLIC_HOST) -> dict[str, str]:
    return keys.headers(f"ws://{host}{TELNYX_PATH}?{query}", now)


def reason_of(auth: MediaAuth, query: str, headers: dict[str, str], now: datetime = NOW) -> str:
    with pytest.raises(MediaAuthError) as caught:
        auth.verify(TELNYX_PATH, query, headers, now)
    return caught.value.reason


def test_signed_stream_url() -> None:
    assert signed_stream_url("wss", "m.example", "/ws/telnyx", "a=1") == "wss://m.example/ws/telnyx?a=1"
    assert signed_stream_url("ws", "m.example", "/ws/telnyx", "") == "ws://m.example/ws/telnyx"


def test_valid_handshake_returns_claims() -> None:
    keys, call_id = MediaKeys(), uuid.uuid4()
    query = stream_query(call_id, keys.token(call_id, NOW))
    assert auth_for(keys).verify(TELNYX_PATH, query, signed(keys, query), NOW) == MediaClaims(
        call_id, "primary"
    )


def test_warm_proxy_leg_is_parsed() -> None:
    keys, call_id = MediaKeys(), uuid.uuid4()
    query = stream_query(call_id, keys.token(call_id, NOW), leg="warm_proxy")
    assert auth_for(keys).verify(TELNYX_PATH, query, signed(keys, query), NOW).leg == "warm_proxy"


@pytest.mark.parametrize(
    "query",
    [
        "token=abc",
        "call_id=not-a-uuid&token=abc",
        f"call_id={uuid.UUID(int=1)}",
        f"call_id={uuid.UUID(int=1)}&token=",
        f"call_id={uuid.UUID(int=1)}&token=a&token=b",
        f"call_id={uuid.UUID(int=1)}&token=abc&leg=listen",
        f"call_id={uuid.UUID(int=1)}&token=abc&leg=",
    ],
)
def test_bad_query_is_rejected_before_the_signature(query: str) -> None:
    keys = MediaKeys()
    assert reason_of(auth_for(keys), query, {}) == "query"


def test_signature_failures() -> None:
    keys, call_id = MediaKeys(), uuid.uuid4()
    query = stream_query(call_id, keys.token(call_id, NOW))
    auth = auth_for(keys)
    assert reason_of(auth, query, {}) == "missing_headers"
    assert reason_of(auth, query, signed(keys, query, host="other.test:8200")) == "invalid_signature"
    assert reason_of(auth, query + "&x=1", signed(keys, query)) == "invalid_signature"
    assert reason_of(auth, query, signed(MediaKeys(), query)) == "invalid_signature"
    stale = signed(keys, query, now=NOW - timedelta(seconds=301))
    assert reason_of(auth, query, stale) == "stale_timestamp"


def test_scheme_is_part_of_the_signed_url() -> None:
    keys, call_id = MediaKeys(), uuid.uuid4()
    query = stream_query(call_id, keys.token(call_id, NOW))
    assert reason_of(auth_for(keys, scheme="wss"), query, signed(keys, query)) == "invalid_signature"


def test_token_failures() -> None:
    keys, call_id = MediaKeys(), uuid.uuid4()
    auth = auth_for(keys)
    cases = {
        "token_malformed": "abc",
        "token_signature": mint_stream_token(call_id, NOW + timedelta(hours=1), secrets.token_bytes(32)),
        "token_expired": mint_stream_token(call_id, NOW, keys.secret),
        "token_call_mismatch": keys.token(uuid.uuid4(), NOW),
    }
    for reason, token in cases.items():
        query = stream_query(call_id, token)
        assert reason_of(auth, query, signed(keys, query)) == reason


def test_previous_secret_is_accepted() -> None:
    keys, call_id = MediaKeys(), uuid.uuid4()
    previous = secrets.token_bytes(32)
    token = mint_stream_token(call_id, NOW + timedelta(hours=1), previous)
    query = stream_query(call_id, token)
    assert (
        auth_for(keys, previous=previous).verify(TELNYX_PATH, query, signed(keys, query), NOW).call_id
        == call_id
    )


def test_authorization_header_must_equal_the_query_token() -> None:
    keys, call_id = MediaKeys(), uuid.uuid4()
    token = keys.token(call_id, NOW)
    query = stream_query(call_id, token)
    auth = auth_for(keys)
    good = {**signed(keys, query), "Authorization": f"Bearer {token}"}
    assert auth.verify(TELNYX_PATH, query, good, NOW).call_id == call_id
    bad = {**signed(keys, query), "authorization": f"Bearer {keys.token(call_id, NOW, max_duration_min=5)}"}
    assert reason_of(auth, query, bad) == "authorization"
    assert reason_of(auth, query, {**signed(keys, query), "Authorization": token}) == "authorization"


def test_secrets_required() -> None:
    keys = MediaKeys()
    with pytest.raises(ValueError, match="secret"):
        MediaAuth(scheme="ws", host=PUBLIC_HOST, public_key=keys.signing.verify_key, secrets=())
