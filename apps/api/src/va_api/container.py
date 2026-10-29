"""Process-wide dependencies of the api, opened in the app lifespan (no network I/O at startup)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from va_api.auth.api_key_auth import ApiKeyAuthenticator, db_lookup, db_touch
from va_api.rate_limit import RateLimiter
from va_api.settings import ApiSettings
from va_common.checks import Dependencies, TemporalConnector, open_dependencies
from va_common.health import CheckFn
from va_common.service import ReadinessSource
from va_common.time import SystemClock
from va_db.session import Database

__all__ = ["Container", "ContainerFactory", "ReadinessSource", "build_container"]

type ContainerFactory = Callable[[ApiSettings], AbstractAsyncContextManager[ReadinessSource]]


@dataclass(slots=True)
class Container:
    settings: ApiSettings
    engine: AsyncEngine
    redis: Redis
    temporal: TemporalConnector
    dependencies: Dependencies
    database: Database
    authenticator: ApiKeyAuthenticator
    rate_limiter: RateLimiter

    def readiness_checks(self) -> Mapping[str, CheckFn]:
        """Postgres, Redis, Temporal (specs/27-observability.md §9)."""
        return self.dependencies.readiness_checks()


@asynccontextmanager
async def build_container(settings: ApiSettings) -> AsyncIterator[Container]:
    """Create the engines, Redis client, Temporal connector, authenticator, and rate limiter; dispose on exit.

    ``RuntimeError`` when ``API_KEY_PEPPER`` is not configured, before anything is opened.
    """
    hasher = settings.api_key_hasher()
    async with open_dependencies() as deps, AsyncExitStack() as stack:
        engine, redis, temporal = deps.postgres(settings), deps.redis(settings), deps.temporal(settings)
        database = Database.from_settings(settings)
        stack.push_async_callback(database.dispose)
        clock = SystemClock()
        authenticator = ApiKeyAuthenticator(
            redis=redis,
            hasher=hasher,
            lookup=db_lookup(database),
            toucher=db_touch(database),
            clock=clock,
        )
        rate_limiter = RateLimiter(redis, clock)
        yield Container(settings, engine, redis, temporal, deps, database, authenticator, rate_limiter)
