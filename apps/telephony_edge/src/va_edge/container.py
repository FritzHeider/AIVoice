"""Edge dependency container: readiness dependencies, the application database (specs/03 §2), the Redis client
(carrier state, carrier channel, event bus, call limits), the Temporal connector (``CallWorkflow`` signals),
the Telnyx client factory (inbound routing commands, BYO keys through envelope encryption), and the call
limiter (inbound concurrency slots)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis

from va_billing.limits import CallLimiter
from va_common.checks import Dependencies, TemporalConnector, open_dependencies
from va_common.crypto import open_kms_provider
from va_common.health import CheckFn
from va_common.time import SystemClock
from va_db.session import Database
from va_db.telnyx_keys import TelnyxKeyResolver
from va_edge.settings import EdgeSettings
from va_telnyx.factory import TelnyxClientFactory
from va_telnyx.settings import telnyx_factory, telnyx_http

__all__ = ["EdgeContainer", "EdgeContainerFactory", "EdgeDependencies", "build_container"]


class EdgeDependencies(Protocol):
    """What the edge lifespan needs from its container."""

    @property
    def database(self) -> Database: ...

    @property
    def redis(self) -> Redis: ...

    @property
    def temporal(self) -> TemporalConnector: ...

    @property
    def telnyx(self) -> TelnyxClientFactory: ...

    @property
    def limiter(self) -> CallLimiter: ...

    def readiness_checks(self) -> Mapping[str, CheckFn]: ...


type EdgeContainerFactory = Callable[[EdgeSettings], AbstractAsyncContextManager[EdgeDependencies]]


@dataclass(frozen=True, slots=True)
class EdgeContainer:
    dependencies: Dependencies
    database: Database
    redis: Redis
    temporal: TemporalConnector
    telnyx: TelnyxClientFactory
    limiter: CallLimiter

    def readiness_checks(self) -> Mapping[str, CheckFn]:
        return self.dependencies.readiness_checks()


@asynccontextmanager
async def build_container(settings: EdgeSettings) -> AsyncIterator[EdgeDependencies]:
    """Postgres engine, Redis client, and Temporal connector (readiness checks in that order; the client and
    the connector also serve the carrier handler), the ``va_app``/``va_system`` database, the Telnyx client
    factory over the shared pool, and the call limiter; no network I/O until used. Everything is disposed on
    exit.

    ``RuntimeError`` before anything is opened when the local KMS provider is selected and
    ``LOCAL_MASTER_KEY`` is not configured (BYO Telnyx keys are envelope-encrypted).
    """
    async with (
        open_kms_provider(settings) as kms_provider,
        open_dependencies() as deps,
        AsyncExitStack() as stack,
    ):
        deps.postgres(settings)
        redis = deps.redis(settings)
        temporal = deps.temporal(settings)
        database = Database.from_settings(settings)
        stack.push_async_callback(database.dispose)
        clock = SystemClock()
        http = telnyx_http(settings)
        stack.push_async_callback(http.aclose)
        yield EdgeContainer(
            dependencies=deps,
            database=database,
            redis=redis,
            temporal=temporal,
            telnyx=telnyx_factory(
                settings,
                http=http,
                redis=redis,
                resolve_key=TelnyxKeyResolver.for_provider(database, kms_provider),
                clock=clock,
            ),
            limiter=CallLimiter(redis, clock=clock),
        )
