"""HMAC stream tokens for the Telnyx media WebSocket (specs/28-security.md §6.2, specs/06 §4.2).

Layout of the raw token (40 bytes, base64url without padding = 54 characters)::

    call_id (16 bytes, UUID bytes) || exp (8 bytes, big-endian unix seconds)
        || HMAC_SHA256(secret, call_id || exp)[:16]

The worker mints one when it dials (``exp`` = mint time + max duration + :data:`STREAM_TOKEN_GRACE_S`); the
media worker verifies it on the WebSocket upgrade, accepting the current or the previous secret during a
rotation. The signature is checked before the expiry so a forged token learns nothing about the clock.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from va_common.settings import StreamTokenSettings

__all__ = [
    "STREAM_TOKEN_GRACE_S",
    "StreamTokenClaims",
    "StreamTokenError",
    "mint_stream_token",
    "stream_token_expiry",
    "stream_token_secrets",
    "verify_stream_token",
]

STREAM_TOKEN_GRACE_S: Final = 600

_CALL_ID_LEN: Final = 16
_EXP_LEN: Final = 8
_MAC_LEN: Final = 16
_TOKEN_LEN: Final = _CALL_ID_LEN + _EXP_LEN + _MAC_LEN
_ENCODED_LEN: Final = 54

StreamTokenFailure = Literal["malformed", "signature", "expired", "call_mismatch"]


class StreamTokenError(ValueError):
    """A stream token failed verification; ``reason`` says which check refused it."""

    def __init__(self, reason: StreamTokenFailure) -> None:
        super().__init__(f"stream token rejected: {reason}")
        self.reason: StreamTokenFailure = reason


@dataclass(frozen=True, slots=True)
class StreamTokenClaims:
    """What a verified token asserts: the call it belongs to and when it stops being accepted."""

    call_id: uuid.UUID
    expires_at: datetime


def stream_token_expiry(now: datetime, max_duration_min: float) -> datetime:
    """``now`` + ``ceil(max_duration_min * 60)`` s + the grace, truncated to whole seconds."""
    seconds = math.ceil(max_duration_min * 60) + STREAM_TOKEN_GRACE_S
    return (now + timedelta(seconds=seconds)).replace(microsecond=0)


def _mac(secret: bytes, signed: bytes) -> bytes:
    return hmac.new(secret, signed, hashlib.sha256).digest()[:_MAC_LEN]


def mint_stream_token(call_id: uuid.UUID, expires_at: datetime, secret: bytes) -> str:
    """The base64url (unpadded) token binding ``call_id`` to ``expires_at`` (whole unix seconds)."""
    signed = call_id.bytes + int(expires_at.timestamp()).to_bytes(_EXP_LEN, "big")
    return base64.urlsafe_b64encode(signed + _mac(secret, signed)).rstrip(b"=").decode("ascii")


def _decode(token: str) -> bytes:
    if len(token) != _ENCODED_LEN:
        raise StreamTokenError("malformed")
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii") + b"==")
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise StreamTokenError("malformed") from None
    if len(raw) != _TOKEN_LEN:  # padding inside the 54 characters decodes to fewer bytes
        raise StreamTokenError("malformed")
    return raw


def verify_stream_token(
    token: str, *, secrets: Sequence[bytes], now: datetime, call_id: uuid.UUID | None = None
) -> StreamTokenClaims:
    """The claims of ``token`` when one of ``secrets`` signed it and it has not expired at ``now``.

    Raises :class:`StreamTokenError` with ``malformed`` (not a 40-byte base64url token), ``signature`` (no
    secret matches), ``expired`` (``exp <= now``), or ``call_mismatch`` (``call_id`` given and different).
    """
    raw = _decode(token)
    signed, mac = raw[: _CALL_ID_LEN + _EXP_LEN], raw[_CALL_ID_LEN + _EXP_LEN :]
    if not any(hmac.compare_digest(_mac(secret, signed), mac) for secret in secrets):
        raise StreamTokenError("signature")
    exp = int.from_bytes(signed[_CALL_ID_LEN:], "big")
    if exp <= now.timestamp():
        raise StreamTokenError("expired")
    claims = StreamTokenClaims(
        call_id=uuid.UUID(bytes=signed[:_CALL_ID_LEN]), expires_at=datetime.fromtimestamp(exp, UTC)
    )
    if call_id is not None and claims.call_id != call_id:
        raise StreamTokenError("call_mismatch")
    return claims


def stream_token_secrets(settings: StreamTokenSettings) -> tuple[bytes, ...]:
    """The configured secrets as bytes: the current one first, then the previous one when set."""
    values = (settings.stream_token_secret, settings.stream_token_secret_previous)
    return tuple(bytes.fromhex(raw) for value in values if (raw := value.get_secret_value()))
