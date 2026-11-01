"""Media WebSocket stream tokens (specs/28-security.md §6.2 Decision P05-T06a)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import string
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from service_helpers import settings_of

from va_common.settings import StreamTokenSettings
from va_common.stream_tokens import (
    STREAM_TOKEN_GRACE_S,
    StreamTokenClaims,
    StreamTokenError,
    mint_stream_token,
    stream_token_expiry,
    stream_token_secrets,
    verify_stream_token,
)

NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
EXP = NOW + timedelta(minutes=40)
URL_SAFE = frozenset(string.ascii_letters + string.digits + "-_")


@pytest.fixture
def secret() -> bytes:
    return os.urandom(32)


def _raw(token: str) -> bytes:
    return base64.urlsafe_b64decode(token + "==")


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _reason(token: str, secret: bytes, *, now: datetime = NOW, call_id: uuid.UUID | None = None) -> str:
    with pytest.raises(StreamTokenError) as caught:
        verify_stream_token(token, secrets=[secret], now=now, call_id=call_id)
    return caught.value.reason


def test_a_minted_token_verifies_to_its_claims(secret: bytes) -> None:
    call_id = uuid.uuid4()
    token = mint_stream_token(call_id, EXP, secret)

    assert len(token) == 54
    assert set(token) <= URL_SAFE
    assert verify_stream_token(token, secrets=[secret], now=NOW, call_id=call_id) == StreamTokenClaims(
        call_id=call_id, expires_at=EXP
    )


def test_the_layout_is_call_id_expiry_and_a_truncated_hmac(secret: bytes) -> None:
    call_id = uuid.uuid4()
    raw = _raw(mint_stream_token(call_id, EXP, secret))

    assert len(raw) == 40
    assert raw[:16] == call_id.bytes
    assert int.from_bytes(raw[16:24], "big") == int(EXP.timestamp())
    assert raw[24:] == hmac.new(secret, raw[:24], hashlib.sha256).digest()[:16]


def test_sub_second_expiry_is_signed_as_whole_seconds(secret: bytes) -> None:
    call_id = uuid.uuid4()
    token = mint_stream_token(call_id, EXP + timedelta(microseconds=999_999), secret)

    assert verify_stream_token(token, secrets=[secret], now=NOW).expires_at == EXP


@pytest.mark.parametrize("position", range(40))
def test_any_flipped_byte_fails_the_signature(secret: bytes, position: int) -> None:
    raw = bytearray(_raw(mint_stream_token(uuid.uuid4(), EXP, secret)))
    raw[position] ^= 0x01

    assert _reason(_encode(bytes(raw)), secret) == "signature"


@pytest.mark.parametrize(
    "mangle",
    [
        lambda token: token[:-1],
        lambda token: token + "A",
        lambda _token: "",
        lambda token: "!" + token[1:],
        lambda token: "é" + token[1:],
        lambda token: token[:-2] + "=" * 2,
    ],
    ids=["truncated", "extra", "empty", "bad-alphabet", "non-ascii", "padded"],
)
def test_wrong_shapes_are_malformed(secret: bytes, mangle: object) -> None:
    assert callable(mangle)
    token = mangle(mint_stream_token(uuid.uuid4(), EXP, secret))

    assert _reason(token, secret) == "malformed"


def test_a_token_expires_at_its_exp(secret: bytes) -> None:
    token = mint_stream_token(uuid.uuid4(), EXP, secret)

    assert verify_stream_token(token, secrets=[secret], now=EXP - timedelta(seconds=1))
    assert _reason(token, secret, now=EXP) == "expired"
    assert _reason(token, secret, now=EXP + timedelta(hours=1)) == "expired"


def test_the_signature_is_checked_before_the_expiry(secret: bytes) -> None:
    token = mint_stream_token(uuid.uuid4(), EXP, os.urandom(32))

    assert _reason(token, secret, now=EXP + timedelta(hours=1)) == "signature"


def test_the_previous_secret_is_accepted_and_unknown_secrets_are_not(secret: bytes) -> None:
    previous = os.urandom(32)
    token = mint_stream_token(uuid.uuid4(), EXP, previous)

    assert verify_stream_token(token, secrets=[secret, previous], now=NOW)
    with pytest.raises(StreamTokenError, match="signature"):
        verify_stream_token(token, secrets=[secret, os.urandom(32)], now=NOW)
    with pytest.raises(StreamTokenError, match="signature"):
        verify_stream_token(token, secrets=[], now=NOW)


def test_a_token_of_another_call_is_a_mismatch(secret: bytes) -> None:
    token = mint_stream_token(uuid.uuid4(), EXP, secret)

    assert _reason(token, secret, call_id=uuid.uuid4()) == "call_mismatch"


@pytest.mark.parametrize(
    ("minutes", "seconds"),
    [(30, 1800 + STREAM_TOKEN_GRACE_S), (0.5, 630), (1 / 60, 601), (0.01, 601), (1.505, 691)],
)
def test_expiry_is_max_duration_plus_ten_minutes_in_whole_seconds(minutes: float, seconds: int) -> None:
    now = NOW + timedelta(microseconds=250_000)

    assert stream_token_expiry(now, minutes) == NOW + timedelta(seconds=seconds)


def test_secrets_come_current_first_and_skip_an_empty_previous() -> None:
    current, previous = secrets.token_hex(32), secrets.token_hex(32)

    assert stream_token_secrets(settings_of(StreamTokenSettings, stream_token_secret=current)) == (
        bytes.fromhex(current),
    )
    both = settings_of(
        StreamTokenSettings, stream_token_secret=current, stream_token_secret_previous=previous.upper()
    )
    assert stream_token_secrets(both) == (bytes.fromhex(current), bytes.fromhex(previous))
    assert stream_token_secrets(settings_of(StreamTokenSettings)) == ()


@pytest.mark.parametrize("field", ["stream_token_secret", "stream_token_secret_previous"])
@pytest.mark.parametrize("length", [63, 64, 65])
def test_secrets_must_be_64_hex_and_errors_never_echo_them(field: str, length: int) -> None:
    value = ("z" + secrets.token_hex(40))[:length]
    with pytest.raises(ValidationError) as caught:
        settings_of(StreamTokenSettings, **{field: value})

    message = str(caught.value)
    assert f"{field.upper()} must be 64 hex characters" in message
    assert value not in message


def test_secrets_are_hidden_in_the_settings_repr() -> None:
    value = secrets.token_hex(32)
    settings = settings_of(StreamTokenSettings, stream_token_secret=value)

    assert value not in repr(settings)
    assert settings.stream_token_secret.get_secret_value() == value
