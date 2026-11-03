"""Outbound webhook signing and request headers (specs/19-events-and-webhooks.md §8; specs/28 §3).

``X-VA-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>`` carries one ``v1`` per active
secret (current first, so receivers keep verifying during a rotation); ``X-Webhook-Signature`` is the
Bland-compatible hex HMAC-SHA256 of the raw body with the current secret. The signed bytes are exactly the
bytes sent.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Sequence
from typing import Final

__all__ = [
    "BLAND_SIGNATURE_HEADER",
    "SIGNATURE_HEADER",
    "USER_AGENT",
    "request_headers",
    "signature_headers",
]

SIGNATURE_HEADER: Final = "X-VA-Signature"
BLAND_SIGNATURE_HEADER: Final = "X-Webhook-Signature"
USER_AGENT: Final = "VoiceAgents-Webhooks/1.0"


def _hex_hmac(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signature_headers(secrets: Sequence[str], body: bytes, timestamp: int) -> dict[str, str]:
    """Both signature headers of ``body`` sent at ``timestamp``; ``ValueError`` without a secret."""
    if not secrets:
        msg = "at least one signing secret is required"
        raise ValueError(msg)
    signed = f"{timestamp}.".encode() + body
    versions = ",".join(f"v1={_hex_hmac(secret, signed)}" for secret in secrets)
    return {
        SIGNATURE_HEADER: f"t={timestamp},{versions}",
        BLAND_SIGNATURE_HEADER: _hex_hmac(secrets[0], body),
    }


def request_headers(
    *, event_id: str, event_type: str, body: bytes, secrets: Sequence[str], timestamp: int
) -> dict[str, str]:
    """Every header of a delivery request (specs/19 §8)."""
    return {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-VA-Event-Id": event_id,
        "X-Event-Id": event_id,
        "X-VA-Event-Type": event_type,
        "X-VA-Timestamp": str(timestamp),
        **signature_headers(secrets, body, timestamp),
    }
