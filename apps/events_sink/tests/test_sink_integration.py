"""The sink end to end on real Redis 7 and ClickHouse 24.8 (testcontainers; specs/19 §1 Decision P06-T05).

One test on purpose: every xdist worker that runs a test of this module boots its own two containers.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import Iterator, Sequence
from functools import partial
from typing import Any, Final

import orjson
import pytest
from docker_probe import docker_available
from redis_container import flushed_redis, running_redis_url
from sink_helpers import ORG, completed_event, turn_event, usage_event_of

from va_common.metrics import Service, build_metrics
from va_common.time import SystemClock
from va_events import EventPublisher, EventType, StreamConsumer, new_event
from va_events.clickhouse import open_clickhouse
from va_events.keys import DEAD_STREAM_KEY
from va_events_sink.schema import SchemaState, bootstrap_schema
from va_events_sink.sink import CONSUMER_GROUP, SinkConfig, StreamSink, run_sink

pytestmark = [pytest.mark.integration, pytest.mark.timeout(240)]

CLICKHOUSE_IMAGE: Final = "clickhouse/clickhouse-server:24.8"


@pytest.fixture(scope="module")
def urls() -> Iterator[tuple[str, str]]:
    if not docker_available():
        pytest.skip("docker unavailable")
    from testcontainers.community.clickhouse import (  # noqa: PLC0415 - imported only when Docker exists
        ClickHouseContainer,
    )

    password = secrets.token_hex(16)
    with (
        running_redis_url() as redis_url,
        ClickHouseContainer(CLICKHOUSE_IMAGE, username="sink", password=password, dbname="sink") as server,
    ):
        host, port = server.get_container_host_ip(), server.get_exposed_port(8123)
        yield redis_url, f"http://sink:{password}@{host}:{port}/sink"


async def _rows(url: str, sql: str, call_id: uuid.UUID) -> list[tuple[Any, ...]]:
    async with open_clickhouse(url) as client:
        result = await client.query(sql, parameters={"call_id": call_id})
        return [tuple(row) for row in result.result_rows]


async def test_events_land_in_every_table_and_poison_is_parked(urls: tuple[str, str]) -> None:
    redis_url, clickhouse_url = urls
    connect = partial(open_clickhouse, clickhouse_url)
    clock = SystemClock()
    schema = SchemaState()
    await bootstrap_schema(connect, schema, clock=clock)
    call_id = uuid.uuid4()
    async with flushed_redis(redis_url, max_connections=32) as redis:
        for stream in ("calls", "billing"):
            await StreamConsumer(redis, stream, CONSUMER_GROUP, "it").ensure_group()
        publisher = EventPublisher(redis, call_events=None)
        latency: dict[str, Any] = {"eot_ms": 310, "e2e_ms": 742, "llm_ttft_ms": 266}
        await publisher.publish(turn_event(call_id, 1, latency=latency, node_id="greeting"))
        await publisher.publish(
            new_event(EventType.CALL_LATENCY, org_id=ORG, clock=clock, call_id=call_id, message="E2E: 742ms")
        )
        await redis.xadd("events:calls", {"payload": b"definitely not an event"})
        await publisher.publish(completed_event(call_id, 3, price=0.5, call_length=2.0))
        await publisher.publish(usage_event_of(reference_id=str(call_id)))

        started: list[StreamSink] = []
        running = asyncio.Event()

        def on_start(sinks: Sequence[StreamSink]) -> None:
            started.extend(sinks)
            running.set()

        config = SinkConfig(consumer="it", streams=("calls", "billing"), batch_interval_s=0.2)
        task = asyncio.create_task(
            run_sink(
                redis,
                connect,
                schema,
                clock=clock,
                metrics=build_metrics(Service.EVENTS_SINK),
                config=config,
                started=on_start,
            )
        )
        try:
            async with asyncio.timeout(60):
                await running.wait()
                for sink in started:
                    await sink.wait_flushes(1)
        finally:
            task.cancel()
            await asyncio.wait({task})

        for key in ("events:calls", "events:billing"):
            assert (await redis.xpending(key, CONSUMER_GROUP))["pending"] == 0
        dead_entries: Any = await redis.xrange(DEAD_STREAM_KEY)
        (dead,) = dead_entries
        assert dead[1][b"reason"] == b"undecodable"
        assert dead[1][b"payload"] == b"definitely not an event"

    events = await _rows(
        clickhouse_url,
        "SELECT seq, category, message, data FROM va.call_events WHERE call_id = {call_id:UUID} ORDER BY seq",
        call_id,
    )
    assert [(seq, category) for seq, category, _, _ in events] == [(1, "call"), (2, "latency"), (3, "call")]
    assert orjson.loads(events[0][3])["latency"] == latency
    turns = await _rows(
        clickhouse_url,
        "SELECT org_id, role, node_id, text_len, eot_ms, stt_final_ms, llm_ttft_ms, e2e_ms "
        "FROM va.call_turns WHERE call_id = {call_id:UUID}",
        call_id,
    )
    assert turns == [(ORG, "assistant", "greeting", len("Hello there"), 310, None, 266, 742)]
    facts = await _rows(
        clickhouse_url,
        "SELECT status, runner_type, connected_seconds, price_micros, ended_at IS NULL "
        "FROM va.calls_fact WHERE call_id = {call_id:UUID}",
        call_id,
    )
    assert facts == [("completed", "task", 120.0, 500_000, 0)]
    usage = await _rows(
        clickhouse_url,
        "SELECT meter, quantity, amount_micros FROM va.usage_events_raw "
        "WHERE reference_id = {call_id:String}",
        call_id,
    )
    assert usage == [("call_connected_seconds", 61.5, 92_250)]
