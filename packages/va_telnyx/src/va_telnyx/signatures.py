"""Telnyx Ed25519 signature verification for webhooks and media WebSockets (specs/06-telephony-telnyx.md §4.1,
§4.2; specs/06 §4.1 Decision P03-T09).

Telnyx signs ``f"{timestamp}|".encode() + signed`` with its Ed25519 key, where ``signed`` is the raw webhook
body bytes or the exact ``stream_url`` we supplied (UTF-8, query string included). The signature arrives
base64-encoded in ``telnyx-signature-ed25519`` and the unix timestamp in ``telnyx-timestamp``. Verification
runs before any JSON parsing; every failure is a :class:`WebhookVerificationError` with a machine-readable
``reason`` (the caller answers 401 or closes the socket with 4401).
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from datetime import datetime
from typing import Final, Literal

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

__all__ = [
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "TOLERANCE_S",
    "VerifyReason",
    "WebhookVerificationError",
    "load_public_key",
    "verify_media_ws",
    "verify_webhook",
]

SIGNATURE_HEADER: Final = "telnyx-signature-ed25519"
TIMESTAMP_HEADER: Final = "telnyx-timestamp"
TOLERANCE_S: Final = 300
_PUBLIC_KEY_BYTES: Final = 32
_SIGNATURE_BYTES: Final = 64
_MAX_TIMESTAMP_DIGITS: Final = 12

type VerifyReason = Literal["missing_headers", "malformed_timestamp", "stale_timestamp", "invalid_signature"]


class WebhookVerificationError(Exception):
    """A webhook or media WebSocket handshake that does not carry a valid, fresh Telnyx signature."""

    def __init__(self, reason: VerifyReason, message: str) -> None:
        super().__init__(message)
        self.reason: VerifyReason = reason


def load_public_key(public_key_b64: str) -> VerifyKey:
    """The Telnyx public key from ``TELNYX_PUBLIC_KEY`` (base64 of the raw 32-byte Ed25519 key).

    Raises ``ValueError`` on a key that is not valid base64 or not 32 bytes (a configuration error).
    """
    try:
        raw = base64.b64decode(public_key_b64.strip(), validate=True)
    except binascii.Error as exc:
        msg = "the Telnyx public key is not valid base64"
        raise ValueError(msg) from exc
    if len(raw) != _PUBLIC_KEY_BYTES:
        msg = f"the Telnyx public key must decode to {_PUBLIC_KEY_BYTES} bytes, got {len(raw)}"
        raise ValueError(msg)
    return VerifyKey(raw)


def verify_webhook(
    raw_body: bytes, headers: Mapping[str, str], public_key: str | VerifyKey, now: datetime
) -> None:
    """Verify a webhook request: signature over ``"{timestamp}|" + raw_body``, timestamp within 300 s of
    ``now``."""
    _verify(raw_body, headers, public_key, now)


def verify_media_ws(
    stream_url: str, headers: Mapping[str, str], public_key: str | VerifyKey, now: datetime
) -> None:
    """Verify a media WebSocket handshake: signature over ``"{timestamp}|{stream_url}"`` (the exact URL we
    supplied, query included), timestamp within 300 s of ``now``."""
    _verify(stream_url.encode("utf-8"), headers, public_key, now)


def _verify(signed: bytes, headers: Mapping[str, str], public_key: str | VerifyKey, now: datetime) -> None:
    if now.tzinfo is None:
        msg = "now must be timezone-aware"
        raise ValueError(msg)
    key = load_public_key(public_key) if isinstance(public_key, str) else public_key
    signature_b64, timestamp = _signature_headers(headers)
    _check_fresh(timestamp, now)
    signature = _decode_signature(signature_b64)
    try:
        key.verify(f"{timestamp}|".encode("ascii") + signed, signature)
    except BadSignatureError as exc:
        raise WebhookVerificationError("invalid_signature", "the Telnyx signature does not verify") from exc


def _signature_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    """(signature, timestamp) header values, matched case-insensitively and stripped."""
    lowered = {name.lower(): value for name, value in headers.items()}
    signature_b64 = lowered.get(SIGNATURE_HEADER, "").strip()
    timestamp = lowered.get(TIMESTAMP_HEADER, "").strip()
    if not signature_b64 or not timestamp:
        raise WebhookVerificationError("missing_headers", "the Telnyx signature headers are missing")
    return signature_b64, timestamp


def _check_fresh(timestamp: str, now: datetime) -> None:
    # The length cap keeps attacker-sized digit strings away from int() limits and float overflow.
    if not (timestamp.isascii() and timestamp.isdigit() and len(timestamp) <= _MAX_TIMESTAMP_DIGITS):
        raise WebhookVerificationError("malformed_timestamp", "telnyx-timestamp is not unix seconds")
    if abs(now.timestamp() - int(timestamp)) > TOLERANCE_S:
        raise WebhookVerificationError(
            "stale_timestamp", f"telnyx-timestamp is more than {TOLERANCE_S} s from the current time"
        )


def _decode_signature(signature_b64: str) -> bytes:
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except binascii.Error as exc:
        raise WebhookVerificationError("invalid_signature", "the signature is not valid base64") from exc
    if len(signature) != _SIGNATURE_BYTES:
        raise WebhookVerificationError("invalid_signature", "the signature is not 64 bytes")
    return signature
