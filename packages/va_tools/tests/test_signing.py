"""Tool request signing (specs/13 §5 step 4): the specs/19 §8 headers, verified independently."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime

import pytest

from va_common.time import FrozenClock
from va_events.webhook_signing import BLAND_SIGNATURE_HEADER, SIGNATURE_HEADER
from va_tools.signing import tool_signer


def _hmac(secret: str, data: bytes) -> str:
    return hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()


def test_signature_covers_timestamp_and_body_with_every_secret() -> None:
    current, previous = secrets.token_hex(16), secrets.token_hex(16)
    clock = FrozenClock(datetime(2026, 11, 5, 12, 0, tzinfo=UTC))
    body = b'{"date":"2026-11-06"}'
    headers = tool_signer([current, previous], clock)(body)
    stamp = int(clock.now().timestamp())
    signed = f"{stamp}.".encode() + body
    assert headers[SIGNATURE_HEADER] == f"t={stamp},v1={_hmac(current, signed)},v1={_hmac(previous, signed)}"
    assert headers[BLAND_SIGNATURE_HEADER] == _hmac(current, body)


def test_signer_needs_a_secret() -> None:
    with pytest.raises(ValueError, match="secret"):
        tool_signer([], FrozenClock(datetime(2026, 11, 5, tzinfo=UTC)))
