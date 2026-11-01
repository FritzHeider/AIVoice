"""The limit scripts on a real Redis 7 under concurrency (specs/24 §13)."""

from __future__ import annotations

import asyncio

import pytest
from redis.asyncio import Redis
from redis_container import flushed_redis, running_redis_url

from va_billing.scripts import LimitScripts

pytestmark = [pytest.mark.integration, pytest.mark.timeout(180)]


async def test_real_redis_limit_scripts() -> None:
    with running_redis_url() as url:
        async with flushed_redis(url, max_connections=100) as redis:
            await exercise(redis)


async def exercise(redis: Redis) -> None:
    scripts = LimitScripts(redis)

    results = await asyncio.gather(*(scripts.capped_incr("cap", 50, 60) for _ in range(100)))
    assert sum(result.ok for result in results) == 50
    assert max(result.value for result in results) == 50
    assert await redis.get("cap") == b"50"
    assert 0 < await redis.ttl("cap") <= 60

    held = await asyncio.gather(
        *(
            scripts.concurrency_acquire("conc", f"call-{n}", 10, now_ms=1_000, expiry_ms=10_000)
            for n in range(100)
        )
    )
    assert sum(held) == 10
    assert await redis.zcard("conc") == 10

    # Every member expired at 10 000 ms: the next acquire self-heals the set.
    assert await scripts.concurrency_acquire("conc", "late", 10, now_ms=10_000, expiry_ms=20_000)
    assert await redis.zrange("conc", 0, -1) == [b"late"]
    assert await redis.zrem("conc", "late") == 1
    assert await redis.zrem("conc", "late") == 0

    releases = await asyncio.gather(*(scripts.counter_release("cap") for _ in range(60)))
    assert min(releases) == 0
    assert await redis.get("cap") == b"0"
