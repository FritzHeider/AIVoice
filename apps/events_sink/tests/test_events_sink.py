"""Events sink settings, probes (specs/27-observability.md §9: Redis, ClickHouse, schema), and entry point."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import respx
from fakeredis import FakeAsyncRedis
from service_helpers import UNREACHABLE, FakeContainer, capture_serve, make_client, ok_check, settings_of
from sink_helpers import FakeConnect, RecordingClock, never_connects, turn_event

from va_common.service import Bind
from va_events import encode
from va_events_sink import __version__
from va_events_sink import main as sink_main
from va_events_sink.container import SinkContainer, build_container
from va_events_sink.main import create_app
from va_events_sink.settings import EventsSinkSettings


def test_defaults_match_spec() -> None:
    settings = settings_of(EventsSinkSettings)
    assert settings.service_name == "events-sink"
    assert settings.clickhouse_url == "http://default:@localhost:8123/va"
    assert settings.redis_url == "redis://localhost:6379/0"


async def test_probes_and_metrics() -> None:
    container = FakeContainer({"redis": ok_check, "clickhouse": ok_check})
    app = create_app(
        settings_of(EventsSinkSettings),
        container_factory=container,
        init_observability=False,
        schema_connect=FakeConnect(),
        sink=None,
    )
    async with make_client(app) as client:
        await asyncio.wait_for(app.state.schema.wait(), timeout=5)
        live, ready = await client.get("/healthz"), await client.get("/readyz")
        metrics = await client.get("/metrics")
    assert live.json() == {"status": "ok", "version": __version__}
    assert ready.json()["checks"] == {"redis": "ok", "clickhouse": "ok", "clickhouse_schema": "ok"}
    assert "# TYPE va_events_sink_inserted_total counter" in metrics.text


@respx.mock
async def test_readyz_real_container_clickhouse_ok_redis_unreachable() -> None:
    route = respx.get("http://clickhouse.test:8123/ping").mock(return_value=httpx.Response(200, text="Ok.\n"))
    settings = settings_of(
        EventsSinkSettings,
        redis_url=UNREACHABLE["redis_url"],
        clickhouse_url="http://default:secret@clickhouse.test:8123/va",
    )
    app = create_app(settings, init_observability=False, schema_connect=never_connects)
    async with make_client(app) as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == {"redis": "error", "clickhouse": "ok", "clickhouse_schema": "error"}
    assert route.call_count == 1


async def test_readyz_real_container_clickhouse_unreachable() -> None:
    settings = settings_of(EventsSinkSettings, clickhouse_url=UNREACHABLE["clickhouse_url"])
    app = create_app(settings, init_observability=False)
    async with make_client(app) as client:
        response = await client.get("/readyz")
    assert {"clickhouse", "clickhouse_schema"} <= set(response.json()["failing"])
    assert not app.state.stop.requested


async def test_build_container_rejects_native_clickhouse_url() -> None:
    with pytest.raises(ValueError, match="http"):
        async with build_container(settings_of(EventsSinkSettings, clickhouse_url="clickhouse://ch:9000/va")):
            pass


def test_main_parses_args_and_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = capture_serve(monkeypatch, sink_main)
    assert sink_main.main(["--port", "19500"]) == 0
    assert [bind for _, bind, _ in seen] == [Bind("0.0.0.0", 19500)]  # noqa: S104
    (app, _, kwargs) = seen[0]
    assert kwargs["stop"] is app.state.stop


@dataclass
class RedisContainer:
    redis: FakeAsyncRedis

    def readiness_checks(self) -> dict[str, Any]:
        return {"redis": ok_check, "clickhouse": ok_check}


async def test_the_lifespan_runs_the_default_sink_after_the_schema() -> None:
    redis = FakeAsyncRedis()
    call_id = uuid.uuid4()
    await redis.xgroup_create("events:calls", "events-sink", id="0", mkstream=True)
    await redis.xadd("events:calls", {"payload": encode(turn_event(call_id, 1))})

    @asynccontextmanager
    async def factory(_: EventsSinkSettings) -> AsyncIterator[RedisContainer]:
        yield RedisContainer(redis)

    connect = FakeConnect()
    app = create_app(
        settings_of(EventsSinkSettings),
        container_factory=factory,
        init_observability=False,
        schema_connect=connect,
        clock=RecordingClock(),
    )
    async with make_client(app) as client:
        async with asyncio.timeout(5):
            while not connect.client.rows_of("call_turns"):
                connect.client.inserted.clear()
                await connect.client.inserted.wait()
        metrics = await client.get("/metrics")
    assert connect.client.rows_of("call_turns")[0][1] == call_id
    assert 'va_events_sink_inserted_total{table="call_turns"} 1.0' in metrics.text
    assert not app.state.stop.requested


async def test_build_container_exposes_the_redis_client() -> None:
    async with build_container(settings_of(EventsSinkSettings)) as container:
        assert isinstance(container, SinkContainer)
        assert list(container.readiness_checks()) == ["redis", "clickhouse"]
        assert container.redis is not None
