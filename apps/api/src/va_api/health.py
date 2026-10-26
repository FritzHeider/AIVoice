"""Readiness checks of the api's dependencies (specs/27-observability.md §9: Postgres, Redis, Temporal)."""

from __future__ import annotations

from typing import Final, Protocol

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from va_common.redis import redis_ping

__all__ = ["CHECK_TIMEOUT_S", "TemporalCheck", "postgres_check", "redis_check", "temporal_check"]

CHECK_TIMEOUT_S: Final = 1.0


class TemporalCheck(Protocol):
    async def check(self, timeout_s: float) -> None: ...


async def postgres_check(engine: AsyncEngine) -> None:
    """``SELECT 1`` on a pooled connection; raises when Postgres is unreachable."""
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def redis_check(client: Redis) -> None:
    """Redis ``PING``; raises ``ConnectionError`` when it does not answer."""
    if not await redis_ping(client, timeout_s=CHECK_TIMEOUT_S):
        msg = "redis did not answer PING"
        raise ConnectionError(msg)


async def temporal_check(connector: TemporalCheck) -> None:
    """Temporal client connected and the workflow service healthy."""
    await connector.check(CHECK_TIMEOUT_S)
