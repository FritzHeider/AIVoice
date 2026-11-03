"""Short-lived transcript WebSocket tokens (specs/19-events-and-webhooks.md §9; Decision P06-T06b).

A token is ``vtt_`` + 43 URL-safe characters. Redis keeps only its SHA-256 under
``transcript:token:{<sha256 hex>}`` (``SET ... EX 60``) with the org, the call, and the expiry; the raw token
is never stored. A token is not single use: any number of WebSocket connections to its call may open with it
until it expires, which is checked both by the key's TTL and against the injected clock.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_common.errors import AppError
from va_common.time import Clock

__all__ = [
    "TOKENS_UNAVAILABLE_MESSAGE",
    "TOKEN_PREFIX",
    "TRANSCRIPT_TOKEN_TTL_S",
    "MintedToken",
    "TranscriptTokenError",
    "TranscriptTokenRecord",
    "TranscriptTokens",
    "token_key",
]

TRANSCRIPT_TOKEN_TTL_S: Final = 60
TOKEN_PREFIX: Final = "vtt_"  # noqa: S105 - a public prefix, not a secret
TOKENS_UNAVAILABLE_MESSAGE: Final = "Transcript tokens are temporarily unavailable; retry shortly."
_TOKEN_BYTES: Final = 32
_MAX_TOKEN_LENGTH: Final = 128

type RefusalReason = Literal["missing", "unknown", "expired", "call_mismatch"]


def token_key(token: str) -> str:
    """The Redis key of ``token`` (its SHA-256, never the token itself)."""
    return f"transcript:token:{hashlib.sha256(token.encode()).hexdigest()}"


class TranscriptTokenRecord(BaseModel):
    """What a token grants: one call of one org until ``expires_at``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: uuid.UUID
    call_id: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class MintedToken:
    """A freshly minted token and when it expires."""

    token: str
    expires_at: datetime


class TranscriptTokenError(Exception):
    """A token was refused (``reason``: ``missing``, ``unknown``, ``expired``, or ``call_mismatch``)."""

    def __init__(self, reason: RefusalReason) -> None:
        super().__init__(f"transcript token refused: {reason}")
        self.reason: RefusalReason = reason


class TranscriptTokens:
    """Mints and resolves transcript tokens in Redis."""

    def __init__(self, redis: Redis, clock: Clock) -> None:
        self._redis = redis
        self._clock = clock

    async def mint(self, org_id: uuid.UUID, call_id: uuid.UUID) -> MintedToken:
        """A new token for ``call_id``; ``SERVICE_UNAVAILABLE`` when Redis fails."""
        token = TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)
        expires_at = self._clock.now() + timedelta(seconds=TRANSCRIPT_TOKEN_TTL_S)
        record = TranscriptTokenRecord(org_id=org_id, call_id=call_id, expires_at=expires_at)
        try:
            await self._redis.set(token_key(token), record.model_dump_json(), ex=TRANSCRIPT_TOKEN_TTL_S)
        except (RedisError, OSError) as exc:
            raise AppError("SERVICE_UNAVAILABLE", TOKENS_UNAVAILABLE_MESSAGE) from exc
        return MintedToken(token, expires_at)

    async def resolve(self, token: str | None, call_id: str) -> TranscriptTokenRecord:
        """The record of ``token`` when it is live and was minted for ``call_id`` (the raw path segment);
        :class:`TranscriptTokenError` otherwise, ``SERVICE_UNAVAILABLE`` when Redis fails."""
        if not token:
            raise TranscriptTokenError("missing")
        if not token.startswith(TOKEN_PREFIX) or len(token) > _MAX_TOKEN_LENGTH:
            raise TranscriptTokenError("unknown")
        try:
            raw = await self._redis.get(token_key(token))
        except (RedisError, OSError) as exc:
            raise AppError("SERVICE_UNAVAILABLE", TOKENS_UNAVAILABLE_MESSAGE) from exc
        if raw is None:
            raise TranscriptTokenError("unknown")
        try:
            record = TranscriptTokenRecord.model_validate_json(raw)
        except ValidationError as exc:
            raise TranscriptTokenError("unknown") from exc
        if record.expires_at <= self._clock.now():
            raise TranscriptTokenError("expired")
        if not _same_call(record.call_id, call_id):
            raise TranscriptTokenError("call_mismatch")
        return record


def _same_call(granted: uuid.UUID, requested: str) -> bool:
    try:
        return uuid.UUID(requested) == granted
    except ValueError:
        return False
