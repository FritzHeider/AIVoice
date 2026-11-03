"""Dependencies of the events sink: Redis and ClickHouse (specs/27-observability.md §9 readiness)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass

from redis.asyncio import Redis

from va_common.checks import Dependencies, open_dependencies
from va_common.health import CheckFn
from va_events_sink.settings import EventsSinkSettings

__all__ = ["SinkContainer", "build_container"]


@dataclass(frozen=True, slots=True)
class SinkContainer:
    """The dependency set and the Redis client the stream consumers read with."""

    dependencies: Dependencies
    redis: Redis

    def readiness_checks(self) -> Mapping[str, CheckFn]:
        """``redis`` and ``clickhouse`` (``GET /ping``), in that order."""
        return self.dependencies.readiness_checks()


@asynccontextmanager
async def build_container(settings: EventsSinkSettings) -> AsyncIterator[SinkContainer]:
    """The Redis client and a ClickHouse HTTP client (no network I/O until a check runs)."""
    async with open_dependencies() as deps:
        redis = deps.redis(settings)
        deps.clickhouse(settings, settings.clickhouse_url)
        yield SinkContainer(deps, redis)
