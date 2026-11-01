"""Telnyx Ed25519 verification of webhooks and media WebSocket handshakes (specs/06 §4.1, §4.2, §13).

The fuzz case covers specs/06 §14 criterion 4 (zero unverified webhooks accepted).
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest
from nacl.signing import SigningKey, VerifyKey

from va_telnyx import WebhookVerificationError, load_public_key, verify_media_ws, verify_webhook
from va_telnyx.signatures import SIGNATURE_HEADER, TIMESTAMP_HEADER, TOLERANCE_S

# A fixed vector: the Ed25519 key of the obviously-test seed bytes(range(32)) signed this body and URL at this
# timestamp. A change to the signed-string format ("{timestamp}|" + bytes) makes these fail.
VECTOR_PUBLIC_KEY: Final = "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg="
VECTOR_TIMESTAMP: Final = "1793534400"
VECTOR_NOW: Final = datetime.fromtimestamp(int(VECTOR_TIMESTAMP), UTC)
VECTOR_BODY: Final = (
    b'{"data":{"record_type":"event","id":"0ccc7b54-4df3-4bca-a65a-3da1ecc777f0","event_type":"call.answered",'
    b'"occurred_at":"2026-11-01T12:00:00.000000Z","payload":{"call_control_id":"v3:vector"}},'
    b'"meta":{"attempt":1,"delivered_to":"https://edge.test/telnyx/voice"}}'
)
VECTOR_BODY_SIGNATURE: Final = (
    "C7uXPMrpijvyXH0n2A6PS0lXRkisYtXXBwjiesHgpTzk23ofF6dib8miiewzJEkfjqmaKtBrxKIBalD7bXM4Cg=="
)
VECTOR_URL: Final = "wss://media.test/ws/telnyx?call_id=1&token=abc"
VECTOR_URL_SIGNATURE: Final = (
    "/i30E8FMbUYyNORrCd69RiQtwVigCwgGG/usXTmYxv9+/+VkkdUTyGjRQm/EhH3v+EH91YGYAdxCzUpOEmO+Ag=="
)
NOW: Final = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
BODY: Final = b'{"data":{"event_type":"call.hangup","id":"e1"}}'


def _headers(signature: str, timestamp: str) -> dict[str, str]:
    return {SIGNATURE_HEADER: signature, TIMESTAMP_HEADER: timestamp}


def _signed(key: SigningKey, message: bytes, at: datetime = NOW) -> dict[str, str]:
    timestamp = str(int(at.timestamp()))
    signature = key.sign(f"{timestamp}|".encode() + message).signature
    return _headers(base64.b64encode(signature).decode(), timestamp)


def _public(key: SigningKey) -> str:
    return base64.b64encode(bytes(key.verify_key)).decode()


@pytest.fixture
def key() -> SigningKey:
    return SigningKey.generate()


# Fixed vectors.


def test_fixed_vector_seed_gives_the_pinned_public_key() -> None:
    assert _public(SigningKey(bytes(range(32)))) == VECTOR_PUBLIC_KEY


def test_fixed_webhook_vector_verifies_and_matches_pynacl() -> None:
    headers = _headers(VECTOR_BODY_SIGNATURE, VECTOR_TIMESTAMP)
    verify_webhook(VECTOR_BODY, headers, VECTOR_PUBLIC_KEY, VECTOR_NOW)
    VerifyKey(base64.b64decode(VECTOR_PUBLIC_KEY)).verify(
        f"{VECTOR_TIMESTAMP}|".encode() + VECTOR_BODY, base64.b64decode(VECTOR_BODY_SIGNATURE)
    )


def test_fixed_vector_rejects_a_one_byte_tampered_body() -> None:
    tampered = VECTOR_BODY.replace(b"call.answered", b"call.answereD")
    with pytest.raises(WebhookVerificationError) as error:
        verify_webhook(
            tampered, _headers(VECTOR_BODY_SIGNATURE, VECTOR_TIMESTAMP), VECTOR_PUBLIC_KEY, VECTOR_NOW
        )
    assert error.value.reason == "invalid_signature"


def test_fixed_media_vector_verifies_only_with_the_exact_url() -> None:
    headers = _headers(VECTOR_URL_SIGNATURE, VECTOR_TIMESTAMP)
    verify_media_ws(VECTOR_URL, headers, VECTOR_PUBLIC_KEY, VECTOR_NOW)
    with pytest.raises(WebhookVerificationError):
        verify_media_ws(VECTOR_URL.replace("call_id=1", "call_id=2"), headers, VECTOR_PUBLIC_KEY, VECTOR_NOW)


# Webhooks.


def test_valid_webhook_with_base64_key_or_verify_key(key: SigningKey) -> None:
    headers = _signed(key, BODY)
    verify_webhook(BODY, headers, _public(key), NOW)
    verify_webhook(BODY, headers, key.verify_key, NOW)
    verify_webhook(BODY, headers, load_public_key(_public(key)), NOW)


def test_header_names_are_case_insensitive(key: SigningKey) -> None:
    headers = {name.upper(): value for name, value in _signed(key, BODY).items()}
    verify_webhook(BODY, headers, _public(key), NOW)


def test_wrong_key_is_rejected(key: SigningKey) -> None:
    with pytest.raises(WebhookVerificationError) as error:
        verify_webhook(BODY, _signed(key, BODY), _public(SigningKey.generate()), NOW)
    assert error.value.reason == "invalid_signature"


def test_tampered_body_is_rejected(key: SigningKey) -> None:
    headers = _signed(key, BODY)
    with pytest.raises(WebhookVerificationError) as error:
        verify_webhook(BODY + b" ", headers, _public(key), NOW)
    assert error.value.reason == "invalid_signature"


def test_signature_does_not_cover_a_different_timestamp(key: SigningKey) -> None:
    headers = _signed(key, BODY)
    headers[TIMESTAMP_HEADER] = str(int(headers[TIMESTAMP_HEADER]) + 1)
    with pytest.raises(WebhookVerificationError) as error:
        verify_webhook(BODY, headers, _public(key), NOW)
    assert error.value.reason == "invalid_signature"


@pytest.mark.parametrize("skew_s", [TOLERANCE_S, -TOLERANCE_S, 0])
def test_timestamps_within_five_minutes_are_accepted(key: SigningKey, skew_s: int) -> None:
    verify_webhook(BODY, _signed(key, BODY, NOW + timedelta(seconds=skew_s)), _public(key), NOW)


@pytest.mark.parametrize("skew_s", [TOLERANCE_S + 1, -(TOLERANCE_S + 1), 86_400])
def test_stale_or_future_timestamps_are_rejected(key: SigningKey, skew_s: int) -> None:
    headers = _signed(key, BODY, NOW + timedelta(seconds=skew_s))
    with pytest.raises(WebhookVerificationError) as error:
        verify_webhook(BODY, headers, _public(key), NOW)
    assert error.value.reason == "stale_timestamp"


@pytest.mark.parametrize("missing", [SIGNATURE_HEADER, TIMESTAMP_HEADER])
def test_missing_headers_are_rejected(key: SigningKey, missing: str) -> None:
    headers = _signed(key, BODY)
    del headers[missing]
    with pytest.raises(WebhookVerificationError) as error:
        verify_webhook(BODY, headers, _public(key), NOW)
    assert error.value.reason == "missing_headers"


def test_empty_headers_count_as_missing(key: SigningKey) -> None:
    with pytest.raises(WebhookVerificationError) as error:
        verify_webhook(BODY, _headers("", ""), _public(key), NOW)
    assert error.value.reason == "missing_headers"


@pytest.mark.parametrize("timestamp", ["1793534400.5", "-1", "abc", "\uff11\uff17\uff19\uff13"])
def test_malformed_timestamps_are_rejected(key: SigningKey, timestamp: str) -> None:
    headers = _signed(key, BODY)
    headers[TIMESTAMP_HEADER] = timestamp
    with pytest.raises(WebhookVerificationError) as error:
        verify_webhook(BODY, headers, _public(key), NOW)
    assert error.value.reason == "malformed_timestamp"


MEDIA_URL = "wss://media.test/ws/telnyx?call_id=1"


def _verify_body(headers: dict[str, str], key: SigningKey) -> None:
    verify_webhook(BODY, headers, _public(key), NOW)


def _verify_url(headers: dict[str, str], key: SigningKey) -> None:
    verify_media_ws(MEDIA_URL, headers, _public(key), NOW)


@pytest.mark.parametrize("digits", [13, 400, 5000])
@pytest.mark.parametrize(("verify", "signed"), [(_verify_body, BODY), (_verify_url, MEDIA_URL.encode())])
def test_oversized_timestamps_are_malformed_not_crashes(
    key: SigningKey, digits: int, verify: Callable[[dict[str, str], SigningKey], None], signed: bytes
) -> None:
    headers = _signed(key, signed)
    headers[TIMESTAMP_HEADER] = "9" * digits
    with pytest.raises(WebhookVerificationError) as error:
        verify(headers, key)
    assert error.value.reason == "malformed_timestamp"


def test_twelve_digit_timestamp_is_parsed_and_judged_stale(key: SigningKey) -> None:
    headers = _signed(key, BODY)
    headers[TIMESTAMP_HEADER] = "9" * 12
    with pytest.raises(WebhookVerificationError) as error:
        verify_webhook(BODY, headers, _public(key), NOW)
    assert error.value.reason == "stale_timestamp"


@pytest.mark.parametrize(
    "signature", ["not base64!", base64.b64encode(b"short").decode(), base64.b64encode(bytes(65)).decode()]
)
def test_undecodable_or_wrong_length_signatures_are_rejected(key: SigningKey, signature: str) -> None:
    headers = _signed(key, BODY)
    headers[SIGNATURE_HEADER] = signature
    with pytest.raises(WebhookVerificationError) as error:
        verify_webhook(BODY, headers, _public(key), NOW)
    assert error.value.reason == "invalid_signature"


def test_naive_now_is_a_programming_error(key: SigningKey) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        verify_webhook(BODY, _signed(key, BODY), _public(key), NOW.replace(tzinfo=None))


def test_error_message_carries_no_signature_material(key: SigningKey) -> None:
    headers = _signed(key, BODY)
    with pytest.raises(WebhookVerificationError) as error:
        verify_webhook(b"other", headers, _public(key), NOW)
    assert headers[SIGNATURE_HEADER] not in str(error.value)


@pytest.mark.parametrize("index", range(200))
def test_random_signatures_are_never_accepted(key: SigningKey, index: int) -> None:
    """specs/06 §14 criterion 4: zero unverified webhooks accepted (fuzz)."""
    timestamp = str(int(NOW.timestamp()))
    signature = base64.b64encode(os.urandom(64)).decode()
    body = BODY + str(index).encode()
    with pytest.raises(WebhookVerificationError) as error:
        verify_webhook(body, _headers(signature, timestamp), _public(key), NOW)
    assert error.value.reason == "invalid_signature"


# Media WebSocket handshakes.


def test_media_signature_over_the_url_with_query(key: SigningKey) -> None:
    url = "wss://media.test/ws/telnyx?call_id=0192f3a0&token=tok"
    headers = _signed(key, url.encode())
    verify_media_ws(url, headers, _public(key), NOW)
    with pytest.raises(WebhookVerificationError) as changed:
        verify_media_ws(url.replace("token=tok", "token=tak"), headers, _public(key), NOW)
    assert changed.value.reason == "invalid_signature"


def test_media_signature_without_the_query_does_not_verify(key: SigningKey) -> None:
    url = "wss://media.test/ws/telnyx?call_id=1"
    headers = _signed(key, b"wss://media.test/ws/telnyx")
    with pytest.raises(WebhookVerificationError) as error:
        verify_media_ws(url, headers, _public(key), NOW)
    assert error.value.reason == "invalid_signature"


def test_media_stale_timestamp_is_rejected(key: SigningKey) -> None:
    url = "wss://media.test/ws/telnyx?call_id=1"
    headers = _signed(key, url.encode(), NOW - timedelta(seconds=TOLERANCE_S + 1))
    with pytest.raises(WebhookVerificationError) as error:
        verify_media_ws(url, headers, _public(key), NOW)
    assert error.value.reason == "stale_timestamp"


# Public key loading.


@pytest.mark.parametrize(
    "value", ["not base64!", base64.b64encode(bytes(31)).decode(), base64.b64encode(bytes(33)).decode()]
)
def test_load_public_key_rejects_bad_keys(value: str) -> None:
    with pytest.raises(ValueError, match="Telnyx public key"):
        load_public_key(value)


def test_load_public_key_strips_whitespace(key: SigningKey) -> None:
    assert bytes(load_public_key(f" {_public(key)}\n")) == bytes(key.verify_key)
