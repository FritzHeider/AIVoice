from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import fakeredis
import pytest
from obs_helpers import make_settings, memory_provider
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from va_common.redis import (
    REDIS_CONNECT_TIMEOUT_S,
    REDIS_HEALTH_CHECK_INTERVAL_S,
    REDIS_SOCKET_TIMEOUT_S,
    redis_client,
    redis_ping,
)


@pytest.fixture
async def fake() -> AsyncIterator[fakeredis.FakeAsyncRedis]:
    client = fakeredis.FakeAsyncRedis()
    yield client
    await client.aclose()


async def test_client_from_settings_carries_timeouts_and_name() -> None:
    client = redis_client(make_settings(redis_url="redis://cache.internal:6380/2"), max_connections=7)
    try:
        kwargs = client.connection_pool.connection_kwargs
        assert kwargs["host"] == "cache.internal"
        assert kwargs["port"] == 6380
        assert kwargs["db"] == 2
        assert kwargs["socket_timeout"] == REDIS_SOCKET_TIMEOUT_S
        assert kwargs["socket_connect_timeout"] == REDIS_CONNECT_TIMEOUT_S
        assert kwargs["health_check_interval"] == REDIS_HEALTH_CHECK_INTERVAL_S
        assert kwargs["client_name"] == "telephony_edge"
        assert kwargs["decode_responses"] is False
        assert client.connection_pool.max_connections == 7
    finally:
        await client.aclose()


async def test_url_override_and_decode_responses() -> None:
    client = redis_client(make_settings(), url="redis://sim-state:6379/5", decode_responses=True)
    try:
        kwargs = client.connection_pool.connection_kwargs
        assert (kwargs["host"], kwargs["db"], kwargs["decode_responses"]) == ("sim-state", 5, True)
    finally:
        await client.aclose()


@pytest.mark.parametrize("url", ["", "   "])
def test_empty_url_is_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="REDIS_URL is empty"):
        redis_client(make_settings(redis_url=url))
    with pytest.raises(ValueError, match="REDIS_URL is empty"):
        redis_client(make_settings(), url=url)


async def test_ping_true_against_live_server(fake: fakeredis.FakeAsyncRedis) -> None:
    assert await redis_ping(fake) is True


class FailingRedis(Redis):
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    async def ping(self, **_: object) -> bool:
        raise self.error


class HangingRedis(Redis):
    def __init__(self) -> None:
        super().__init__()
        self.never = asyncio.Event()

    async def ping(self, **_: object) -> bool:
        await self.never.wait()
        return True


@pytest.mark.parametrize(
    "error", [RedisConnectionError("refused"), OSError("unreachable")], ids=["redis", "os"]
)
async def test_ping_false_on_errors(error: BaseException) -> None:
    client = FailingRedis(error)
    try:
        assert await redis_ping(client) is False
    finally:
        await client.aclose()


async def test_ping_false_on_timeout() -> None:
    client = HangingRedis()
    try:
        assert await redis_ping(client, timeout_s=0.01) is False
    finally:
        await client.aclose()


async def test_tracer_provider_instruments_commands() -> None:
    provider, exporter = memory_provider()
    client = redis_client(make_settings(), tracer_provider=provider)
    server = fakeredis.FakeServer()
    client.connection_pool = fakeredis.FakeAsyncRedis(server=server).connection_pool
    try:
        await client.set("k", "v")
        assert await client.get("k") == b"v"
    finally:
        await client.aclose()
    names = [span.name for span in exporter.get_finished_spans()]
    assert names == ["SET", "GET"]


async def test_uninstrumented_without_tracer_provider() -> None:
    provider, exporter = memory_provider()
    client = redis_client(make_settings())
    client.connection_pool = fakeredis.FakeAsyncRedis().connection_pool
    try:
        await client.set("k", "v")
    finally:
        await client.aclose()
    assert exporter.get_finished_spans() == ()
    provider.shutdown()
