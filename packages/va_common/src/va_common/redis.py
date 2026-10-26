"""Redis client factory and health probe (specs/02 §6 ``REDIS_URL``, specs/27-observability.md §2 and §9).

Every service builds its Redis client with :func:`redis_client` so timeouts, health checks, the client name,
and OpenTelemetry instrumentation are uniform. Never construct ``redis.asyncio.Redis`` directly.
"""

from __future__ import annotations

import asyncio
from typing import Final

from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.trace import TracerProvider
from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_common.settings import RedisSettings

__all__ = [
    "REDIS_CONNECT_TIMEOUT_S",
    "REDIS_HEALTH_CHECK_INTERVAL_S",
    "REDIS_SOCKET_TIMEOUT_S",
    "redis_client",
    "redis_ping",
]

REDIS_SOCKET_TIMEOUT_S: Final = 5.0
REDIS_CONNECT_TIMEOUT_S: Final = 2.0
REDIS_HEALTH_CHECK_INTERVAL_S: Final = 30


def redis_client(
    settings: RedisSettings,
    *,
    url: str | None = None,
    decode_responses: bool = False,
    max_connections: int = 50,
    tracer_provider: TracerProvider | None = None,
) -> Redis:
    """Build an asyncio Redis client from ``REDIS_URL`` (or ``url``, e.g. ``SIM_STATE_REDIS_URL``).

    The client has socket/connect timeouts, periodic connection health checks, retry on timeout, and the
    service name as ``CLIENT SETNAME``. With ``tracer_provider`` every command emits an OpenTelemetry span.
    """
    target = (url if url is not None else settings.redis_url).strip()
    if not target:
        msg = "REDIS_URL is empty"
        raise ValueError(msg)
    client: Redis = Redis.from_url(
        target,
        socket_timeout=REDIS_SOCKET_TIMEOUT_S,
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT_S,
        health_check_interval=REDIS_HEALTH_CHECK_INTERVAL_S,
        retry_on_timeout=True,
        max_connections=max_connections,
        decode_responses=decode_responses,
        client_name=settings.service_name,
    )
    if tracer_provider is not None:
        RedisInstrumentor().instrument_client(client, tracer_provider=tracer_provider)
    return client


async def redis_ping(client: Redis, *, timeout_s: float = 1.0) -> bool:
    """Return whether Redis answers ``PING`` within ``timeout_s`` seconds; never raises (readiness probes)."""
    try:
        async with asyncio.timeout(timeout_s):
            await client.ping()
    except (TimeoutError, RedisError, OSError):
        return False
    return True
