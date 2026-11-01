"""A throwaway Redis 7 container for ``@pytest.mark.integration`` tests (skips when Docker is unavailable)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

import pytest
from docker_probe import docker_available
from redis.asyncio import Redis
from service_helpers import settings_of

from va_common.redis import redis_client
from va_common.settings import RedisSettings

__all__ = ["flushed_redis", "running_redis_url"]


@contextmanager
def running_redis_url() -> Iterator[str]:
    """Yield ``redis://host:port/0`` of a fresh ``redis:7.2-alpine`` container (skips without Docker)."""
    if not docker_available():
        pytest.skip("docker unavailable")
    from testcontainers.community.redis import RedisContainer  # noqa: PLC0415 - imported only when Docker exists

    with RedisContainer("redis:7.2-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@asynccontextmanager
async def flushed_redis(url: str, *, max_connections: int) -> AsyncIterator[Redis]:
    """A ``redis_client`` for ``url`` on an emptied database, closed on exit."""
    client = redis_client(settings_of(RedisSettings, redis_url=url), max_connections=max_connections)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()
