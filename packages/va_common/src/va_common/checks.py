"""Readiness checks of service dependencies and the stack that opens them (specs/27-observability.md §9).

Each service opens its dependencies with :func:`open_dependencies`; every ``Dependencies.<kind>()`` call
creates a client without network I/O, registers its cleanup, and adds the matching readiness check (named
``postgres``, ``redis``, ``temporal``, ``clickhouse``) in call order.
Extra checks join with :meth:`Dependencies.add_check`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from functools import partial
from typing import Final, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from temporalio.client import Client

from va_common.health import CheckFn
from va_common.http import http_client
from va_common.redis import redis_client, redis_ping
from va_common.settings import BaseServiceSettings, DatabaseSettings, RedisSettings, TemporalSettings

__all__ = [
    "CHECK_TIMEOUT_S",
    "CLICKHOUSE_PING_BODY",
    "Dependencies",
    "TemporalCheck",
    "TemporalConnector",
    "clickhouse_check",
    "clickhouse_ping_url",
    "create_engine",
    "open_dependencies",
    "postgres_check",
    "redis_check",
    "temporal_check",
]

CHECK_TIMEOUT_S: Final = 1.0
CLICKHOUSE_PING_BODY: Final = "Ok."
_HTTP_SCHEMES: Final = frozenset({"http", "https"})

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


async def clickhouse_check(client: httpx.AsyncClient, ping_url: str) -> None:
    """ClickHouse HTTP ``GET /ping`` answers ``Ok.``; raises otherwise."""
    response = await client.get(ping_url)
    response.raise_for_status()
    if response.text.strip() != CLICKHOUSE_PING_BODY:
        msg = "clickhouse /ping returned an unexpected body"
        raise ConnectionError(msg)


def clickhouse_ping_url(clickhouse_url: str) -> str:
    """``CLICKHOUSE_URL`` reduced to scheme, host, port, and ``/ping`` (credentials and database dropped)."""
    parts = urlsplit(clickhouse_url)
    if parts.scheme not in _HTTP_SCHEMES or not parts.hostname:
        msg = "CLICKHOUSE_URL must be an http(s) URL with a host"
        raise ValueError(msg)
    netloc = parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, "/ping", "", ""))


def create_engine(settings: DatabaseSettings) -> AsyncEngine:
    """Async SQLAlchemy engine for ``DATABASE_URL``; connects lazily on first use."""
    return create_async_engine(
        settings.database_url, pool_size=settings.database_pool_size, pool_pre_ping=True
    )


class Dependencies:
    """Clients opened by :func:`open_dependencies` and the readiness checks they contribute."""

    def __init__(self, stack: AsyncExitStack) -> None:
        self._stack = stack
        self._checks: dict[str, CheckFn] = {}

    def add_check(self, name: str, check: CheckFn) -> None:
        if name in self._checks:
            msg = f"readiness check {name!r} is already registered"
            raise ValueError(msg)
        self._checks[name] = check

    def readiness_checks(self) -> dict[str, CheckFn]:
        return dict(self._checks)

    def postgres(self, settings: DatabaseSettings) -> AsyncEngine:
        engine = create_engine(settings)
        self._stack.push_async_callback(engine.dispose)
        self.add_check("postgres", partial(postgres_check, engine))
        return engine

    def redis(self, settings: RedisSettings, *, url: str | None = None) -> Redis:
        client = redis_client(settings, url=url)
        self._stack.push_async_callback(client.aclose)
        self.add_check("redis", partial(redis_check, client))
        return client

    def temporal(self, settings: TemporalSettings) -> TemporalConnector:
        connector = TemporalConnector(settings.temporal_host, settings.temporal_namespace)
        self.add_check("temporal", partial(temporal_check, connector))
        return connector

    def clickhouse(self, settings: BaseServiceSettings, clickhouse_url: str) -> httpx.AsyncClient:
        ping_url = clickhouse_ping_url(clickhouse_url)
        client = http_client(settings, timeout=CHECK_TIMEOUT_S)
        self._stack.push_async_callback(client.aclose)
        self.add_check("clickhouse", partial(clickhouse_check, client, ping_url))
        return client


@asynccontextmanager
async def open_dependencies() -> AsyncIterator[Dependencies]:
    """Yield an empty :class:`Dependencies`; what it opened is closed in reverse order on exit."""
    async with AsyncExitStack() as stack:
        yield Dependencies(stack)
