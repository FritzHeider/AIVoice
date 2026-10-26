from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from obs_helpers import make_settings

from va_common.redis import redis_client, redis_ping

pytestmark = pytest.mark.integration

_DEFAULT_SOCKETS = ("/var/run/docker.sock", str(Path.home() / ".docker/run/docker.sock"))


def _reachable(address: str) -> bool:
    """Connect to the Docker daemon endpoint without the docker SDK (which leaks sockets on failure)."""
    parts = urlsplit(address)
    try:
        if parts.scheme == "tcp":
            with socket.create_connection((parts.hostname or "localhost", parts.port or 2375), timeout=1):
                return True
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            sock.connect(parts.path or address)
            return True
    except OSError:
        return False


def _docker_available() -> bool:
    host = os.environ.get("DOCKER_HOST")
    candidates = [host] if host else [f"unix://{path}" for path in _DEFAULT_SOCKETS]
    return any(_reachable(candidate) for candidate in candidates)


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    if not _docker_available():
        pytest.skip("docker unavailable")
    from testcontainers.redis import RedisContainer  # noqa: PLC0415 - imported only when Docker exists

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
