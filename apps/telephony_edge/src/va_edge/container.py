"""Edge dependency container: readiness dependencies, the application database (specs/03 §2), the Redis client
(carrier state, carrier channel, event bus), and the Temporal connector (``CallWorkflow`` signals)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis

from va_common.checks import Dependencies, TemporalConnector, open_dependencies
from va_common.health import CheckFn
from va_db.session import Database
from va_edge.settings import EdgeSettings

__all__ = ["EdgeContainer", "EdgeContainerFactory", "EdgeDependencies", "build_container"]


class EdgeDependencies(Protocol):
    """What the edge lifespan needs from its container."""

    @property
    def database(self) -> Database: ...

    @property
    def redis(self) -> Redis: ...

    @property
    def temporal(self) -> TemporalConnector: ...

    def readiness_checks(self) -> Mapping[str, CheckFn]: ...


type EdgeContainerFactory = Callable[[EdgeSettings], AbstractAsyncContextManager[EdgeDependencies]]


@dataclass(frozen=True, slots=True)
class EdgeContainer:
    dependencies: Dependencies
    database: Database
    redis: Redis
    temporal: TemporalConnector

    def readiness_checks(self) -> Mapping[str, CheckFn]:
        return self.dependencies.readiness_checks()


@asynccontextmanager
async def build_container(settings: EdgeSettings) -> AsyncIterator[EdgeDependencies]:
    """Postgres engine, Redis client, and Temporal connector (readiness checks in that order; the client and
    the connector also serve the carrier handler), and the ``va_app``/``va_system`` database; no network I/O
    until used. Everything is disposed on exit."""
    async with open_dependencies() as deps, AsyncExitStack() as stack:
        deps.postgres(settings)
        redis = deps.redis(settings)
        temporal = deps.temporal(settings)
        database = Database.from_settings(settings)
        stack.push_async_callback(database.dispose)
        yield EdgeContainer(dependencies=deps, database=database, redis=redis, temporal=temporal)
