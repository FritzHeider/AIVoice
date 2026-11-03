"""Transcript WebSocket tokens (specs/19-events-and-webhooks.md §9 Decision P06-T06b): minting, the hashed
Redis key with a 60 s TTL, and every refusal reason."""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

import fakeredis
import pytest

from va_api.services.transcript_tokens import (
    TOKEN_PREFIX,
    TRANSCRIPT_TOKEN_TTL_S,
    TranscriptTokenError,
    TranscriptTokenRecord,
    TranscriptTokens,
    token_key,
)
from va_common.errors import AppError
from va_common.time import FrozenClock


@pytest.fixture
def redis() -> fakeredis.FakeAsyncRedis:
    return fakeredis.FakeAsyncRedis()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def tokens(redis: fakeredis.FakeAsyncRedis, clock: FrozenClock) -> TranscriptTokens:
    return TranscriptTokens(redis, clock)


async def test_a_minted_token_is_stored_hashed_with_a_sixty_second_ttl(
    tokens: TranscriptTokens, redis: fakeredis.FakeAsyncRedis, clock: FrozenClock
) -> None:
    org_id, call_id = uuid.uuid4(), uuid.uuid4()

    minted = await tokens.mint(org_id, call_id)

    assert minted.token.startswith(TOKEN_PREFIX)
    assert len(minted.token) == len(TOKEN_PREFIX) + 43
    assert minted.expires_at == clock.now() + timedelta(seconds=TRANSCRIPT_TOKEN_TTL_S)
    key = f"transcript:token:{hashlib.sha256(minted.token.encode()).hexdigest()}"
    assert token_key(minted.token) == key
    assert await redis.keys("*") == [key.encode()]
    assert 0 < await redis.ttl(key) <= TRANSCRIPT_TOKEN_TTL_S
    raw = await redis.get(key)
    assert isinstance(raw, bytes)
    stored = TranscriptTokenRecord.model_validate_json(raw)
    assert stored == TranscriptTokenRecord(org_id=org_id, call_id=call_id, expires_at=minted.expires_at)
    assert minted.token.encode() not in raw


async def test_tokens_are_unique(tokens: TranscriptTokens) -> None:
    call_id = uuid.uuid4()
    minted = {(await tokens.mint(uuid.uuid4(), call_id)).token for _ in range(20)}
    assert len(minted) == 20


async def test_a_live_token_resolves_for_its_call_any_number_of_times(tokens: TranscriptTokens) -> None:
    org_id, call_id = uuid.uuid4(), uuid.uuid4()
    minted = await tokens.mint(org_id, call_id)

    for path_id in (str(call_id), str(call_id).upper(), str(call_id)):
        record = await tokens.resolve(minted.token, path_id)
        assert (record.org_id, record.call_id) == (org_id, call_id)


async def _refusal(tokens: TranscriptTokens, token: str | None, call_id: str) -> str:
    with pytest.raises(TranscriptTokenError) as caught:
        await tokens.resolve(token, call_id)
    return caught.value.reason


async def test_refusal_reasons(
    tokens: TranscriptTokens, redis: fakeredis.FakeAsyncRedis, clock: FrozenClock
) -> None:
    call_id = uuid.uuid4()
    minted = await tokens.mint(uuid.uuid4(), call_id)
    path = str(call_id)

    assert await _refusal(tokens, None, path) == "missing"
    assert await _refusal(tokens, "", path) == "missing"
    assert await _refusal(tokens, "not-a-token", path) == "unknown"
    assert await _refusal(tokens, TOKEN_PREFIX + "x" * 200, path) == "unknown"
    assert await _refusal(tokens, minted.token + "x", path) == "unknown"
    assert await _refusal(tokens, minted.token, str(uuid.uuid4())) == "call_mismatch"
    assert await _refusal(tokens, minted.token, "not-a-uuid") == "call_mismatch"
    await redis.set(token_key(TOKEN_PREFIX + "garbled"), b"{not json")
    assert await _refusal(tokens, TOKEN_PREFIX + "garbled", path) == "unknown"

    clock.advance(timedelta(seconds=TRANSCRIPT_TOKEN_TTL_S))
    assert await _refusal(tokens, minted.token, path) == "expired"


class _BrokenRedis:
    async def set(self, key: str, value: str, *, ex: int) -> None:
        raise ConnectionRefusedError(key, value, ex)

    async def get(self, key: str) -> None:
        raise ConnectionRefusedError(key)


async def test_redis_failures_are_service_unavailable(clock: FrozenClock) -> None:
    broken = TranscriptTokens(_BrokenRedis(), clock)  # type: ignore[arg-type]
    for attempt in (broken.mint(uuid.uuid4(), uuid.uuid4()), broken.resolve(TOKEN_PREFIX + "a", "x")):
        with pytest.raises(AppError) as caught:
            await attempt
        assert caught.value.code == "SERVICE_UNAVAILABLE"
