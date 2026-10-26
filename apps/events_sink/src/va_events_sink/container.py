"""Dependencies of the events sink: Redis and ClickHouse (specs/27-observability.md §9 readiness)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from va_common.checks import Dependencies, open_dependencies
from va_events_sink.settings import EventsSinkSettings

__all__ = ["build_container"]


@asynccontextmanager
async def build_container(settings: EventsSinkSettings) -> AsyncIterator[Dependencies]:
    """The Redis client and a ClickHouse HTTP client (no network I/O until a check runs)."""
    async with open_dependencies() as deps:
        deps.redis(settings)
        deps.clickhouse(settings, settings.clickhouse_url)
        yield deps
