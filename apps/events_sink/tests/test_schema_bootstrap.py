"""ClickHouse schema bootstrap of the events sink: retries, readiness, and fatal errors."""

from __future__ import annotations

import asyncio
import io
from typing import Any

import pytest
from ch_support import FakeClickHouse
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError
from service_helpers import FakeContainer, json_log_stream, log_events, make_client, ok_check, settings_of
from sink_helpers import FakeConnect, RecordingClock

from va_common.service import StopController
from va_events_sink.main import create_app
from va_events_sink.schema import SchemaState, bootstrap_schema
from va_events_sink.settings import EventsSinkSettings


async def test_state_check_fails_until_applied() -> None:
    state = SchemaState()
    with pytest.raises(ConnectionError, match="not applied"):
        await state.check()
    state.mark_applied()
    await state.check()
    await asyncio.wait_for(state.wait(), timeout=1)
    assert state.applied


def transient_failures() -> FakeConnect:
    return FakeConnect(failures=[OperationalError("Network Error"), ConnectionRefusedError()])


async def test_transient_errors_are_retried_with_doubling_backoff() -> None:
    connect, state, clock = transient_failures(), SchemaState(), RecordingClock()
    await bootstrap_schema(connect, state, clock=clock)
    assert state.applied
    assert clock.sleeps == [0.5, 1.0]
    assert (connect.opened, connect.closed) == (3, 1)
    assert connect.client.recorded.keys() == {"0001"}


def logged(stream: io.StringIO, event: str, *keys: str) -> list[tuple[object, ...]]:
    return [tuple(e[key] for key in keys) for e in log_events(stream) if e["event"] == event]


async def test_retries_and_readiness_are_logged() -> None:
    stream = json_log_stream()
    await bootstrap_schema(transient_failures(), SchemaState(), clock=RecordingClock())
    assert logged(stream, "clickhouse.schema_retry", "attempt", "backoff_s", "error") == [
        (1, 0.5, "OperationalError"),
        (2, 1.0, "ConnectionRefusedError"),
    ]
    assert logged(stream, "clickhouse.schema_ready", "attempts", "applied") == [(3, ["0001"])]


async def test_backoff_is_capped() -> None:
    connect = FakeConnect(failures=[OperationalError("down") for _ in range(6)])
    clock = RecordingClock()
    await bootstrap_schema(connect, SchemaState(), clock=clock)
    assert clock.sleeps == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0]


async def test_non_transient_errors_propagate() -> None:
    connect = FakeConnect(failures=[DatabaseError("Syntax error")])
    state, clock = SchemaState(), RecordingClock()
    with pytest.raises(DatabaseError, match="Syntax error"):
        await bootstrap_schema(connect, state, clock=clock)
    assert not state.applied
    assert clock.sleeps == []


def sink_app(connect: Any, stop: StopController | None = None) -> Any:
    return create_app(
        settings_of(EventsSinkSettings),
        container_factory=FakeContainer({"redis": ok_check, "clickhouse": ok_check}),
        init_observability=False,
        schema_connect=connect,
        clock=RecordingClock(),
        stop=stop,
    )


async def test_lifespan_applies_the_schema_then_readiness_passes() -> None:
    connect = FakeConnect()
    app = sink_app(connect)
    async with make_client(app) as client:
        await asyncio.wait_for(app.state.schema.wait(), timeout=5)
        response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["checks"] == {"redis": "ok", "clickhouse": "ok", "clickhouse_schema": "ok"}
    assert connect.client.recorded.keys() == {"0001"}
    assert not app.state.stop.requested


async def test_schema_drift_stops_the_process_with_a_failure() -> None:
    stop = StopController()
    app = sink_app(FakeConnect(client=FakeClickHouse(recorded={"0001": "edited"})), stop=stop)
    async with make_client(app) as client:
        await asyncio.wait_for(stop.wait(), timeout=5)
        response = await client.get("/readyz")
    assert stop.failed
    assert stop.reason == "task_failed:clickhouse_schema"
    assert response.json()["failing"] == ["clickhouse_schema"]
