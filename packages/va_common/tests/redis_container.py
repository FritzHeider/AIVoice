"""A throwaway Redis 7 container for ``@pytest.mark.integration`` tests (skips when Docker is unavailable)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from docker_probe import docker_available

__all__ = ["running_redis_url"]


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
