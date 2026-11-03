"""Outbound webhook signatures and headers (specs/19-events-and-webhooks.md §8)."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from va_webhooks.signing import request_headers, signature_headers

SECRET = "whsec_test"  # noqa: S105 - fixed test vector
BODY = b'{"a":1}'
T = 1700000000

# Fixed vector (HMAC-SHA256 hex), pinned so a change of the signed bytes or encoding is caught.
V1 = "38877139021993b830af32feea6e18a8da83eb2f6e49ee50bd9e4cf4ca4d3789"
BLAND = "51426af50a41dd7ff2cd3f116594734766d4018d15d6fb07169aee5d2959adf5"


def _hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def test_signature_headers_follow_the_spec_formula() -> None:
    headers = signature_headers([SECRET], BODY, T)
    assert headers == {
        "X-VA-Signature": f"t={T},v1={_hex(SECRET, b'1700000000.' + BODY)}",
        "X-Webhook-Signature": _hex(SECRET, BODY),
    }


def test_fixed_vector() -> None:
    assert signature_headers([SECRET], BODY, T) == {
        "X-VA-Signature": f"t={T},v1={V1}",
        "X-Webhook-Signature": BLAND,
    }


def test_two_secrets_give_two_versions_current_first() -> None:
    headers = signature_headers(["whsec_new", "whsec_old"], BODY, T)
    signed = f"{T}.".encode() + BODY
    assert headers["X-VA-Signature"] == f"t={T},v1={_hex('whsec_new', signed)},v1={_hex('whsec_old', signed)}"
    assert headers["X-Webhook-Signature"] == _hex("whsec_new", BODY)


def test_no_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one signing secret"):
        signature_headers([], BODY, T)


def test_request_headers_carry_ids_type_timestamp_and_user_agent() -> None:
    headers = request_headers(
        event_id="evt_1", event_type="call.completed", body=BODY, secrets=[SECRET], timestamp=T
    )
    assert {key: value for key, value in headers.items() if "Signature" not in key} == {
        "Content-Type": "application/json",
        "User-Agent": "VoiceAgents-Webhooks/1.0",
        "X-VA-Event-Id": "evt_1",
        "X-Event-Id": "evt_1",
        "X-VA-Event-Type": "call.completed",
        "X-VA-Timestamp": "1700000000",
    }
    assert headers["X-VA-Signature"].startswith("t=1700000000,v1=")
