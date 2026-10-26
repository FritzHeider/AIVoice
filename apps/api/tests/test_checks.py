"""Real dependency checks and the container (no running services: unreachable ports and fakes)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import fakeredis
import pytest
from api_helpers import make_client, make_settings
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from temporalio.client import Client

from va_api.container import Container, TemporalConnector, build_container
from va_api.health import postgres_check, redis_check, temporal_check
from va_api.main import create_app
from va_common.redis import redis_client

UNREACHABLE_PG = "postgresql+asyncpg://va:va@127.0.0.1:1/va"
UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"
UNREACHABLE_TEMPORAL = "127.0.0.1:1"


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
    client = redis_client(make_settings(redis_url=UNREACHABLE_REDIS))
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


async def test_build_container_opens_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[str] = []
    real_dispose, real_aclose = AsyncEngine.dispose, Redis.aclose

    async def dispose(self: AsyncEngine, close: bool = True) -> None:
        closed.append("engine")
        await real_dispose(self, close)

    async def aclose(self: Redis, close_connection_pool: bool | None = None) -> None:
        closed.append("redis")
        await real_aclose(self, close_connection_pool)

    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    monkeypatch.setattr(Redis, "aclose", aclose)
    settings = make_settings(database_url=UNREACHABLE_PG, redis_url=UNREACHABLE_REDIS, database_pool_size=3)
    async with build_container(settings) as container:
        assert isinstance(container, Container)
        assert container.engine.pool.size() == 3  # type: ignore[attr-defined]
        assert container.temporal.host == settings.temporal_host
        assert container.temporal.namespace == settings.temporal_namespace
        assert set(container.readiness_checks()) == {"postgres", "redis", "temporal"}
        assert closed == []
    assert closed == ["redis", "engine"]


async def test_readyz_real_container_reports_unreachable() -> None:
    settings = make_settings(
        database_url=UNREACHABLE_PG, redis_url=UNREACHABLE_REDIS, temporal_host=UNREACHABLE_TEMPORAL
    )
    app = create_app(settings, init_observability=False, readiness_timeout_s=1.0)
    async with make_client(app) as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["failing"] == ["postgres", "redis", "temporal"]
    assert set(body["checks"].values()) <= {"error", "timeout"}
