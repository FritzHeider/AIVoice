"""Process-wide dependencies of the api, opened in the app lifespan (no network I/O at startup)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from va_api.settings import ApiSettings
from va_common.checks import Dependencies, TemporalConnector, open_dependencies
from va_common.health import CheckFn
from va_common.service import ReadinessSource

__all__ = ["Container", "ContainerFactory", "ReadinessSource", "build_container"]

type ContainerFactory = Callable[[ApiSettings], AbstractAsyncContextManager[ReadinessSource]]


@dataclass(slots=True)
class Container:
    settings: ApiSettings
    engine: AsyncEngine
    redis: Redis
    temporal: TemporalConnector
    dependencies: Dependencies

    def readiness_checks(self) -> Mapping[str, CheckFn]:
        """Postgres, Redis, Temporal (specs/27-observability.md §9)."""
        return self.dependencies.readiness_checks()


@asynccontextmanager
async def build_container(settings: ApiSettings) -> AsyncIterator[Container]:
    """Create the engine, Redis client, and Temporal connector; dispose them on exit."""
    async with open_dependencies() as deps:
        engine, redis, temporal = deps.postgres(settings), deps.redis(settings), deps.temporal(settings)
        yield Container(settings, engine, redis, temporal, deps)
