from __future__ import annotations

import pytest
from fakeredis import FakeAsyncRedis
from redis_ttl import TTL_SLACK_MS, assert_expires_in

KEY = "ttl:probe"


async def test_a_key_set_with_the_ttl_passes() -> None:
    redis = FakeAsyncRedis()
    await redis.set(KEY, "1", ex=100)
    await assert_expires_in(redis, KEY, 100)


async def test_a_key_just_inside_the_slack_passes() -> None:
    redis = FakeAsyncRedis()
    await redis.set(KEY, "1", px=100_000 - TTL_SLACK_MS + 1_000)
    await assert_expires_in(redis, KEY, 100)


async def test_a_shorter_ttl_fails() -> None:
    redis = FakeAsyncRedis()
    await redis.set(KEY, "1", px=100_000 - TTL_SLACK_MS - 1_000)
    with pytest.raises(AssertionError, match="ttl:probe: pttl"):
        await assert_expires_in(redis, KEY, 100)


async def test_a_longer_ttl_fails() -> None:
    redis = FakeAsyncRedis()
    await redis.set(KEY, "1", ex=101)
    with pytest.raises(AssertionError):
        await assert_expires_in(redis, KEY, 100)


async def test_a_key_without_expiry_fails() -> None:
    redis = FakeAsyncRedis()
    await redis.set(KEY, "1")
    with pytest.raises(AssertionError, match="pttl -1"):
        await assert_expires_in(redis, KEY, 100)


async def test_a_missing_key_fails() -> None:
    with pytest.raises(AssertionError, match="pttl -2"):
        await assert_expires_in(FakeAsyncRedis(), KEY, 100)


async def test_a_custom_slack_narrows_the_window() -> None:
    redis = FakeAsyncRedis()
    await redis.set(KEY, "1", px=95_000)
    await assert_expires_in(redis, KEY, 100)
    with pytest.raises(AssertionError):
        await assert_expires_in(redis, KEY, 100, slack_ms=1_000)
