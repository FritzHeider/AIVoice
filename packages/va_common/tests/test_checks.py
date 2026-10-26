"""Readiness checks and the dependency stack (no running services: unreachable ports, fakes, respx)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import fakeredis
import httpx
import pytest
import respx
from redis.asyncio import Redis
from service_helpers import ok_check, settings_of, track_closes
from sqlalchemy.ext.asyncio import create_async_engine
from temporalio.client import Client

from va_common.checks import (
    Dependencies,
    TemporalConnector,
    clickhouse_check,
    clickhouse_ping_url,
    create_engine,
    open_dependencies,
    postgres_check,
    redis_check,
    temporal_check,
)
from va_common.http import http_client
from va_common.redis import redis_client
from va_common.settings import BaseServiceSettings, DatabaseSettings, RedisSettings, TemporalSettings

UNREACHABLE_PG = "postgresql+asyncpg://va:va@127.0.0.1:1/va"
UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"


class FakeServiceClient:
    def __init__(self, healthy: bool) -> None:
        self.healthy = healthy
        self.timeouts: list[timedelta | None] = []

    async def check_health(self, *, timeout: timedelta | None = None) -> bool:  # noqa: ASYNC109 - mirrors temporalio
        self.timeouts.append(timeout)
        return self.healthy


class FakeClient:
    def __init__(self, healthy: bool = True) -> None:
        self.service_client = FakeServiceClient(healthy)


class CountingConnect:
    def __init__(self, *, fail_first: int = 0, healthy: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_first = fail_first
        self.healthy = healthy

    async def __call__(self, host: str, namespace: str) -> Client:
        self.calls.append((host, namespace))
        if len(self.calls) <= self.fail_first:
            msg = "connect refused"
            raise RuntimeError(msg)
        return cast("Client", FakeClient(self.healthy))


async def test_postgres_check_unreachable_raises() -> None:
    engine = create_async_engine(UNREACHABLE_PG)
    try:
        with pytest.raises(OSError):
            await postgres_check(engine)
    finally:
        await engine.dispose()


async def test_redis_check_ok_with_fakeredis() -> None:
    client: Redis = fakeredis.FakeAsyncRedis()
    try:
        await redis_check(client)
    finally:
        await client.aclose()


async def test_redis_check_unreachable_raises() -> None:
    client = redis_client(settings_of(RedisSettings, redis_url=UNREACHABLE_REDIS))
    try:
        with pytest.raises(ConnectionError, match="PING"):
            await redis_check(client)
    finally:
        await client.aclose()


async def test_temporal_connector_caches_client() -> None:
    connect = CountingConnect()
    connector = TemporalConnector("temporal:7233", "ns", connect=connect)
    first = await connector.client()
    assert await connector.client() is first
    assert connect.calls == [("temporal:7233", "ns")]


async def test_temporal_connector_retries_after_failed_connect() -> None:
    connect = CountingConnect(fail_first=1)
    connector = TemporalConnector("t:1", "default", connect=connect)
    with pytest.raises(RuntimeError, match="refused"):
        await connector.client()
    await connector.client()
    assert len(connect.calls) == 2


async def test_temporal_check_healthy_passes_timeout() -> None:
    connector = TemporalConnector("t:1", "default", connect=CountingConnect())
    await temporal_check(connector)
    client: Any = await connector.client()
    assert client.service_client.timeouts == [timedelta(seconds=1)]


async def test_temporal_check_unhealthy_raises() -> None:
    connector = TemporalConnector("t:1", "default", connect=CountingConnect(healthy=False))
    with pytest.raises(ConnectionError, match="not serving"):
        await temporal_check(connector)


PING_URL = "http://clickhouse.test:8123/ping"


class AllSettings(RedisSettings, DatabaseSettings, TemporalSettings):
    """Every dependency's settings in one class."""


@respx.mock
async def test_clickhouse_check_ok() -> None:
    respx.get(PING_URL).mock(return_value=httpx.Response(200, text="Ok.\n"))
    async with http_client(settings_of(BaseServiceSettings), timeout=1.0) as client:
        await clickhouse_check(client, PING_URL)


@respx.mock
async def test_clickhouse_check_bad_body_raises() -> None:
    respx.get(PING_URL).mock(return_value=httpx.Response(200, text="nope"))
    async with http_client(settings_of(BaseServiceSettings), timeout=1.0) as client:
        with pytest.raises(ConnectionError, match="unexpected body"):
            await clickhouse_check(client, PING_URL)


@respx.mock
async def test_clickhouse_check_http_error_raises() -> None:
    respx.get(PING_URL).mock(return_value=httpx.Response(503, text="Ok."))
    async with http_client(settings_of(BaseServiceSettings), timeout=1.0) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await clickhouse_check(client, PING_URL)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://default:@localhost:8123/va", "http://localhost:8123/ping"),
        ("https://user:secret@ch.example.com/analytics?x=1", "https://ch.example.com/ping"),
        ("http://clickhouse", "http://clickhouse/ping"),
    ],
)
def test_clickhouse_ping_url_strips_credentials_and_db(url: str, expected: str) -> None:
    assert clickhouse_ping_url(url) == expected


@pytest.mark.parametrize("url", ["clickhouse://localhost:9000/va", "localhost:8123", "http:///va"])
def test_clickhouse_ping_url_rejects_non_http(url: str) -> None:
    with pytest.raises(ValueError, match="http"):
        clickhouse_ping_url(url)


async def test_create_engine_does_not_connect() -> None:
    engine = create_engine(settings_of(DatabaseSettings, database_url=UNREACHABLE_PG, database_pool_size=4))
    try:
        assert engine.pool.size() == 4  # type: ignore[attr-defined]
        assert engine.pool.checkedout() == 0  # type: ignore[attr-defined]
    finally:
        await engine.dispose()


async def test_dependencies_register_checks_in_call_order_and_close_in_reverse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = track_closes(monkeypatch)
    settings = settings_of(AllSettings, database_url=UNREACHABLE_PG, redis_url=UNREACHABLE_REDIS)
    async with open_dependencies() as deps:
        deps.redis(settings)
        deps.postgres(settings)
        connector = deps.temporal(settings)
        deps.clickhouse(settings, "http://default:@localhost:8123/va")
        deps.add_check("extra", ok_check)
        assert list(deps.readiness_checks()) == ["redis", "postgres", "temporal", "clickhouse", "extra"]
        assert (connector.host, connector.namespace) == ("localhost:7233", "default")
        assert closed == []
    assert closed == ["http", "engine", "redis"]


async def test_dependencies_reject_duplicate_check() -> None:
    async with open_dependencies() as deps:
        deps.add_check("redis", ok_check)
        with pytest.raises(ValueError, match="already registered"):
            deps.redis(settings_of(RedisSettings))
        assert isinstance(deps, Dependencies)


async def test_dependencies_readiness_checks_is_a_copy() -> None:
    async with open_dependencies() as deps:
        deps.readiness_checks()["x"] = ok_check
        assert deps.readiness_checks() == {}
