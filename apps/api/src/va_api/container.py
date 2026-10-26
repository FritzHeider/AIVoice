"""Process-wide dependencies of the api, opened in the app lifespan (no network I/O at startup)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from typing import Protocol

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from temporalio.client import Client

from va_api.health import postgres_check, redis_check, temporal_check
from va_api.settings import ApiSettings
from va_common.health import CheckFn
from va_common.redis import redis_client

__all__ = [
    "Container",
    "ContainerFactory",
    "ReadinessSource",
    "TemporalConnector",
    "build_container",
]

type Connect = Callable[[str, str], Awaitable[Client]]


async def _connect(host: str, namespace: str) -> Client:
    return await Client.connect(host, namespace=namespace)


class TemporalConnector:
    """Lazily connected, cached Temporal client; a failed connect is retried on the next use."""

    def __init__(self, host: str, namespace: str, *, connect: Connect = _connect) -> None:
        self.host = host
        self.namespace = namespace
        self._connect = connect
        self._client: Client | None = None
        self._lock = asyncio.Lock()

    async def client(self) -> Client:
        async with self._lock:
            if self._client is None:
                self._client = await self._connect(self.host, self.namespace)
            return self._client

    async def check(self, timeout_s: float) -> None:
        """Raise ``ConnectionError`` unless the workflow service reports healthy within ``timeout_s``."""
        client = await self.client()
        if not await client.service_client.check_health(timeout=timedelta(seconds=timeout_s)):
            msg = "temporal workflow service is not serving"
            raise ConnectionError(msg)


class ReadinessSource(Protocol):
    def readiness_checks(self) -> dict[str, CheckFn]: ...


type ContainerFactory = Callable[[ApiSettings], AbstractAsyncContextManager[ReadinessSource]]


@dataclass(slots=True)
class Container:
    settings: ApiSettings
    engine: AsyncEngine
    redis: Redis
    temporal: TemporalConnector

    def readiness_checks(self) -> dict[str, CheckFn]:
        return {
            "postgres": partial(postgres_check, self.engine),
            "redis": partial(redis_check, self.redis),
            "temporal": partial(temporal_check, self.temporal),
        }


@asynccontextmanager
async def build_container(settings: ApiSettings) -> AsyncIterator[Container]:
    """Create the engine, Redis client, and Temporal connector; dispose them on exit."""
    engine = create_async_engine(
        settings.database_url, pool_size=settings.database_pool_size, pool_pre_ping=True
    )
    redis = redis_client(settings)
    try:
        yield Container(
            settings, engine, redis, TemporalConnector(settings.temporal_host, settings.temporal_namespace)
        )
    finally:
        await redis.aclose()
        await engine.dispose()
