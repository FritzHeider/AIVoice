from __future__ import annotations

from collections.abc import Iterator

import pytest
from docker_probe import docker_available
from obs_helpers import make_settings

from va_common.redis import redis_client, redis_ping

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
