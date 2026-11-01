"""The limit Lua scripts on fakeredis (real Redis: test_limits_redis.py)."""

from __future__ import annotations

import pytest
from fakeredis import FakeAsyncRedis

from va_billing.scripts import LimitScripts


@pytest.fixture
def redis() -> FakeAsyncRedis:
    return FakeAsyncRedis()


async def test_capped_incr_boundary_and_ttl(redis: FakeAsyncRedis) -> None:
    scripts = LimitScripts(redis)
    first = await scripts.capped_incr("k", 2, 100)
    assert (first.ok, first.value) == (True, 1)
    assert 0 < await redis.ttl("k") <= 100
    await redis.expire("k", 50)
    second = await scripts.capped_incr("k", 2, 100)
    assert (second.ok, second.value) == (True, 2)
    assert await redis.ttl("k") <= 50
    third = await scripts.capped_incr("k", 2, 100)
    assert (third.ok, third.value) == (False, 2)
    assert await redis.get("k") == b"2"


async def test_capped_incr_unlimited_and_zero(redis: FakeAsyncRedis) -> None:
    scripts = LimitScripts(redis)
    for expected in range(1, 6):
        assert (await scripts.capped_incr("u", None, 10)).value == expected
    zero = await scripts.capped_incr("z", 0, 10)
    assert (zero.ok, zero.value) == (False, 0)
    negative = await scripts.capped_incr("n", -3, 10)
    assert negative.ok is False


async def test_counter_release_never_negative_or_created(redis: FakeAsyncRedis) -> None:
    scripts = LimitScripts(redis)
    assert await scripts.counter_release("absent") == 0
    assert await redis.exists("absent") == 0
    await scripts.capped_incr("c", None, 10)
    assert await scripts.counter_release("c") == 0
    assert await scripts.counter_release("c") == 0
    assert await redis.get("c") == b"0"


async def test_concurrency_acquire(redis: FakeAsyncRedis) -> None:
    scripts = LimitScripts(redis)
    assert await scripts.concurrency_acquire("s", "a", 2, now_ms=1000, expiry_ms=5000)
    assert await scripts.concurrency_acquire("s", "b", 2, now_ms=1000, expiry_ms=6000)
    assert not await scripts.concurrency_acquire("s", "c", 2, now_ms=2000, expiry_ms=7000)
    # Re-acquiring a held member only refreshes its expiry, even when the set is full.
    assert await scripts.concurrency_acquire("s", "a", 2, now_ms=2000, expiry_ms=9000)
    assert await redis.zscore("s", "a") == 9000
    assert 0 < await redis.pttl("s") <= 7000
    # At 6000 ms member b has expired and is dropped, which frees a slot.
    assert await scripts.concurrency_acquire("s", "c", 2, now_ms=6000, expiry_ms=8000)
    assert sorted(await redis.zrange("s", 0, -1)) == [b"a", b"c"]


async def test_concurrency_unlimited(redis: FakeAsyncRedis) -> None:
    scripts = LimitScripts(redis)
    for member in ("a", "b", "c"):
        assert await scripts.concurrency_acquire("s", member, None, now_ms=0, expiry_ms=1000)
    assert await redis.zcard("s") == 3
