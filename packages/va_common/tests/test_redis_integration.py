from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from docker_probe import docker_available
from obs_helpers import make_settings

from va_common.redis import redis_client, redis_ping
from va_common.token_bucket import BucketLimit, TokenBucket

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    if not docker_available():
        pytest.skip("docker unavailable")
    from testcontainers.community.redis import RedisContainer  # noqa: PLC0415 - imported only when Docker exists

    with RedisContainer("redis:7.2-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


async def test_round_trip_against_real_redis(redis_url: str) -> None:
    client = redis_client(make_settings(), url=redis_url, decode_responses=True)
    try:
        assert await redis_ping(client) is True
        await client.set("va:it", "ok")
        assert await client.get("va:it") == "ok"
    finally:
        await client.aclose()


async def test_token_bucket_against_real_redis(redis_url: str) -> None:
    client = redis_client(make_settings(), url=redis_url, max_connections=100)
    bucket, limit, now_ms = TokenBucket(client), BucketLimit(rate_per_s=10.0, burst=20), 1_767_225_600_000
    try:
        decisions = await asyncio.gather(
            *(bucket.take("rl:key:{it}:default", limit, now_ms=now_ms) for _ in range(100))
        )
        assert sum(decision.allowed for decision in decisions) == 20
        refilled = await bucket.take("rl:key:{it}:default", limit, now_ms=now_ms + 1_000)
        assert (refilled.allowed, refilled.remaining) == (True, 9)
        assert 3_900 <= await client.pttl("rl:key:{it}:default") <= 4_000
    finally:
        await client.aclose()
