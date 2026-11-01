"""Edge dependency container: readiness dependencies plus the application database (specs/03 §2)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from va_common.checks import Dependencies, open_dependencies
from va_common.health import CheckFn
from va_db.session import Database
from va_edge.settings import EdgeSettings

__all__ = ["EdgeContainer", "EdgeContainerFactory", "EdgeDependencies", "build_container"]


class EdgeDependencies(Protocol):
    """What the edge lifespan needs from its container."""

    @property
    def database(self) -> Database: ...

    def readiness_checks(self) -> Mapping[str, CheckFn]: ...


type EdgeContainerFactory = Callable[[EdgeSettings], AbstractAsyncContextManager[EdgeDependencies]]


@dataclass(frozen=True, slots=True)
class EdgeContainer:
    dependencies: Dependencies
    database: Database

    def readiness_checks(self) -> Mapping[str, CheckFn]:
        return self.dependencies.readiness_checks()


@asynccontextmanager
async def build_container(settings: EdgeSettings) -> AsyncIterator[EdgeDependencies]:
    """Postgres engine, Redis client, and Temporal connector for readiness (in that check order), and the
    ``va_app``/``va_system`` database; no network I/O until used. Everything is disposed on exit."""
    async with open_dependencies() as deps, AsyncExitStack() as stack:
        deps.postgres(settings)
        deps.redis(settings)
        deps.temporal(settings)
        database = Database.from_settings(settings)
        stack.push_async_callback(database.dispose)
        yield EdgeContainer(dependencies=deps, database=database)
