"""The stream sink: batching, acks after inserts, retries, parking, recovery, lag (specs/19 §1 Decision
P06-T05)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import orjson
import pytest
from ch_support import FakeClickHouse
from clickhouse_connect.driver.exceptions import DatabaseError, DataError, OperationalError
from clock_helpers import ManualClock, RecordingClock
from fakeredis import FakeAsyncRedis, FakeServer
from service_helpers import json_log_stream, log_events
from sink_helpers import ORG, FakeConnect, completed_event, turn_event, usage_event_of

from va_common.metrics import Service, ServiceMetrics, build_metrics
from va_common.time import Clock, FrozenClock
from va_events import DomainEvent, EventType, StreamConsumer, decode, encode, new_event
from va_events.keys import DEAD_STREAM_KEY
from va_events_sink.schema import SchemaState
from va_events_sink.sink import SinkConfig, StreamSink, build_sinks, run_sink

CALL = uuid.UUID("0193c5d2-0000-7000-8000-0000000000c2")
GROUP = "events-sink"


class Rig:
    def __init__(self, clock: Clock, **config: Any) -> None:
        self.server = FakeServer()
        self.redis = FakeAsyncRedis(server=self.server)
        self.client = FakeClickHouse()
        self.clock = clock
        self.metrics: ServiceMetrics = build_metrics(Service.EVENTS_SINK)
        settings: dict[str, Any] = {"consumer": "pod-a", "streams": ("calls",), **config}
        self.config = SinkConfig(**settings)
        self.sinks = build_sinks(
            self.redis, self.client, clock=clock, metrics=self.metrics, config=self.config
        )
        self.sink = self.sinks[0]

    async def publish(self, *events: DomainEvent, stream: str = "calls") -> list[str]:
        ids: list[Any] = [await self.redis.xadd(f"events:{stream}", {"payload": encode(e)}) for e in events]
        return [i.decode() for i in ids]

    async def pending(self, stream: str = "calls") -> int:
        return int((await self.redis.xpending(f"events:{stream}", GROUP))["pending"])

    async def dead(self) -> list[dict[str, Any]]:
        entries: Any = await self.redis.xrange(DEAD_STREAM_KEY)
        return [{k.decode(): v for k, v in fields.items()} for _, fields in entries]

    def counter(self, name: str, table: str) -> float:
        return self.metrics.registry.get_sample_value(name, {"table": table}) or 0.0

    async def start(self) -> None:
        await StreamConsumer(self.redis, "calls", GROUP, "pod-a").ensure_group()


def turns(count: int, *, start: int = 1) -> list[DomainEvent]:
    return [turn_event(CALL, seq, role="user", text=f"turn {seq}") for seq in range(start, start + count)]


async def test_a_full_batch_is_flushed_without_waiting() -> None:
    rig = Rig(RecordingClock(), batch_rows=5, batch_interval_s=60.0)
    await rig.start()
    await rig.publish(*turns(7))
    await rig.sink.cycle()
    assert rig.sink.flushes == 1
    assert [row[2] for row in rig.client.rows_of("call_turns")] == [1, 2, 3, 4, 5]
    assert len(rig.client.rows_of("call_events")) == 5
    assert await rig.pending() == 0
    assert rig.counter("va_events_sink_inserted_total", "call_turns") == 5


async def test_a_batch_is_flushed_once_the_interval_passed_on_the_clock() -> None:
    clock = FrozenClock()
    rig = Rig(clock, batch_rows=1000, batch_interval_s=1.0)
    await rig.start()
    await rig.publish(*turns(2))
    await rig.sink.cycle()
    assert (rig.sink.flushes, await rig.pending()) == (0, 2)
    clock.advance(1.0)
    await rig.publish(*turns(1, start=3))
    await rig.sink.cycle()
    assert rig.sink.flushes == 1
    assert len(rig.client.rows_of("call_turns")) == 3
    assert await rig.pending() == 0


async def test_a_quiet_stream_flushes_when_the_window_read_returns_nothing() -> None:
    rig = Rig(RecordingClock(), batch_rows=1000, batch_interval_s=0.01)
    await rig.start()
    await rig.publish(*turns(2))
    await rig.sink.cycle()
    assert rig.sink.flushes == 0
    await rig.sink.cycle()
    assert rig.sink.flushes == 1
    assert await rig.pending() == 0


async def test_an_empty_cycle_flushes_nothing() -> None:
    rig = Rig(RecordingClock(), batch_interval_s=0.01)
    await rig.start()
    await rig.sink.cycle()
    assert (rig.sink.flushes, rig.client.attempts) == (0, 0)


async def test_transient_insert_errors_retry_with_backoff_and_ack_only_after_success() -> None:
    clock = ManualClock()
    rig = Rig(clock, batch_rows=2)
    rig.client.insert_failures = [OperationalError("down"), OSError("reset")]
    stream = json_log_stream()
    await rig.start()
    await rig.publish(*turns(2))
    cycle = asyncio.create_task(rig.sink.cycle())
    await clock.wait_for_sleep(0.5)
    assert await rig.pending() == 2
    await clock.run_for(0.5)
    await clock.wait_for_sleep(1.0)
    assert (await rig.pending(), rig.client.inserts) == (2, [])
    await clock.run_for(1.0)
    await cycle
    assert await rig.pending() == 0
    assert len(rig.client.rows_of("call_events")) == 2
    assert rig.counter("va_events_sink_errors_total", "call_events") == 2
    retries = [e for e in log_events(stream) if e["event"] == "events_sink.insert_retry"]
    assert [(e["attempt"], e["backoff_s"], e["error"]) for e in retries] == [
        (1, 0.5, "OperationalError"),
        (2, 1.0, "OSError"),
    ]
    assert all("rows" in e and e["table"] == "call_events" for e in retries)


async def test_the_backoff_is_capped() -> None:
    clock = RecordingClock()
    rig = Rig(clock, batch_rows=1)
    rig.client.insert_failures = [OperationalError("down") for _ in range(6)]
    await rig.start()
    await rig.publish(*turns(1))
    await rig.sink.cycle()
    assert clock.sleeps == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0]


def _reject_text(text: str) -> Any:
    def reject(table: str, rows: Sequence[Sequence[Any]]) -> BaseException | None:
        if table == "call_turns" and any(row[5] == len(text) for row in rows):
            return DataError("Cannot parse input")
        return None

    return reject


async def test_a_rejected_batch_is_inserted_event_by_event_and_only_the_bad_event_is_parked() -> None:
    rig = Rig(RecordingClock(), batch_rows=3)
    rig.client.reject = _reject_text("an unusually long poison turn")
    await rig.start()
    good_a, good_b = turns(2)
    bad = turn_event(CALL, 3, role="user", text="an unusually long poison turn")
    _, bad_id, _ = await rig.publish(good_a, bad, good_b)
    await rig.sink.cycle()
    assert len(rig.client.rows_of("call_events")) == 3
    assert [row[2] for row in rig.client.rows_of("call_turns")] == [1, 2]
    (parked,) = await rig.dead()
    assert parked["entry_id"].decode() == bad_id
    assert parked["reason"] == b"insert_failed:call_turns"
    assert parked["stream"] == b"events:calls"
    assert decode(parked["payload"]) == bad
    assert await rig.pending() == 0


async def test_a_transient_error_during_the_per_event_fallback_is_retried() -> None:
    clock = RecordingClock()
    rig = Rig(clock, batch_rows=2)
    rig.client.insert_failures = [DatabaseError("Code: 27"), OperationalError("down")]
    await rig.start()
    await rig.publish(*turns(2))
    await rig.sink.cycle()
    assert clock.sleeps == [0.5]
    assert len(rig.client.rows_of("call_events")) == 2
    assert await rig.dead() == []


async def test_unmappable_events_are_parked_and_the_rest_inserted() -> None:
    rig = Rig(RecordingClock(), batch_rows=2)
    await rig.start()
    good = turns(1)[0]
    broken = turn_event(CALL, 2).model_copy(update={"data": {"role": "user"}})
    await rig.publish(broken, good)
    await rig.sink.cycle()
    (parked,) = await rig.dead()
    assert parked["reason"] == b"unmappable:call_turns:invalid_data"
    assert [row[2] for row in rig.client.rows_of("call_turns")] == [1]
    assert await rig.pending() == 0


async def test_events_without_rows_are_acked_without_an_insert() -> None:
    rig = Rig(RecordingClock(), batch_rows=1, streams=("billing",))
    await StreamConsumer(rig.redis, "billing", GROUP, "pod-a").ensure_group()
    low = new_event(EventType.BILLING_LOW_BALANCE, org_id=ORG, clock=FrozenClock())
    await rig.publish(low, stream="billing")
    await rig.sink.cycle()
    assert (rig.client.attempts, await rig.pending("billing")) == (0, 0)


async def test_an_unexpected_insert_error_propagates_and_acks_nothing() -> None:
    rig = Rig(RecordingClock(), batch_rows=1)
    rig.client.insert_failures = [RuntimeError("bug")]
    await rig.start()
    await rig.publish(*turns(1))
    with pytest.raises(RuntimeError, match="bug"):
        await rig.sink.cycle()
    assert await rig.pending() == 1


async def test_every_table_of_a_batch_is_written() -> None:
    rig = Rig(RecordingClock(), batch_rows=3, streams=("calls", "billing"))
    calls_sink, billing_sink = rig.sinks
    for sink_stream in ("calls", "billing"):
        await StreamConsumer(rig.redis, sink_stream, GROUP, "pod-a").ensure_group()
    await rig.publish(turn_event(CALL, 1, latency={"e2e_ms": 700}), completed_event(CALL, 2))
    await rig.publish(usage_event_of(), stream="billing")
    for sink in (calls_sink, calls_sink, billing_sink, billing_sink):
        await sink.cycle()
    assert [insert.table for insert in rig.client.inserts] == [
        "call_events",
        "call_turns",
        "calls_fact",
        "usage_events_raw",
    ]
    assert all(insert.database == "va" for insert in rig.client.inserts)
    (turn_row,) = rig.client.rows_of("call_turns")
    assert turn_row[11] == 700
    (usage_row,) = rig.client.rows_of("usage_events_raw")
    assert usage_row[0] == ORG
    data = orjson.loads(rig.client.rows_of("call_events")[1][6])
    assert data["call_id"] == str(CALL)


async def _running(sink: StreamSink) -> AsyncIterator[asyncio.Task[None]]:
    task = asyncio.create_task(sink.run())
    try:
        yield task
    finally:
        task.cancel()
        await asyncio.wait({task})


async def test_a_restart_rereads_its_own_pending_entries_first() -> None:
    rig = Rig(RecordingClock(), batch_rows=2, batch_interval_s=0.01)
    await rig.start()
    await rig.publish(*turns(3))
    previous_life = StreamConsumer(rig.redis, "calls", GROUP, "pod-a")
    assert len(await previous_life.read(count=3, block_ms=1)) == 3
    async for _ in _running(rig.sink):
        await asyncio.wait_for(rig.sink.wait_flushes(2), timeout=5)
    assert [row[2] for row in rig.client.rows_of("call_turns")] == [1, 2, 3]
    assert await rig.pending() == 0


async def test_a_redis_outage_drops_the_batch_and_restarts_after_a_backoff() -> None:
    clock = ManualClock()
    rig = Rig(clock, batch_rows=1, batch_interval_s=0.01)
    await rig.start()
    stream = json_log_stream()
    rig.server.connected = False
    async for _ in _running(rig.sink):
        await clock.wait_for_sleep(0.5)
        rig.server.connected = True
        await rig.publish(*turns(1))
        await clock.run_for(0.5)
        await asyncio.wait_for(rig.sink.wait_flushes(1), timeout=5)
    assert len(rig.client.rows_of("call_turns")) == 1
    (retry,) = [e for e in log_events(stream) if e["event"] == "events_sink.redis_retry"]
    assert (retry["stream"], retry["attempt"], retry["backoff_s"]) == ("events:calls", 1, 0.5)


async def test_a_failed_park_keeps_the_entry_pending_for_the_next_life() -> None:
    clock = ManualClock()
    rig = Rig(clock, batch_rows=1, batch_interval_s=0.01)
    await rig.start()
    await rig.redis.set(DEAD_STREAM_KEY, "not a stream")
    broken = turn_event(CALL, 1).model_copy(update={"data": {"role": "user"}})
    await rig.publish(broken)
    async for _ in _running(rig.sink):
        await clock.wait_for_sleep(0.5)
        assert await rig.pending() == 1
        await rig.redis.delete(DEAD_STREAM_KEY)
        await clock.run_for(0.5)
        await asyncio.wait_for(rig.sink.wait_flushes(1), timeout=5)
    assert await rig.pending() == 0
    (parked,) = await rig.dead()
    assert parked["reason"] == b"unmappable:call_turns:invalid_data"


async def test_lag_is_published_per_stream() -> None:
    clock = FrozenClock()
    rig = Rig(clock, batch_rows=1000, batch_interval_s=60.0)
    await rig.start()
    (entry_id,) = await rig.publish(*turns(1))
    entry_ms = int(entry_id.split("-")[0])
    clock.advance(entry_ms / 1000 + 4.0 - clock.now().timestamp())
    await rig.sink.cycle()
    lag = rig.metrics.registry.get_sample_value("va_events_sink_lag_seconds", {"stream": "calls"})
    assert lag == pytest.approx(4.0, abs=0.01)


async def test_run_sink_waits_for_the_schema_then_connects_with_retries() -> None:
    server = FakeServer()
    redis = FakeAsyncRedis(server=server)
    connect = FakeConnect(failures=[OperationalError("not yet")])
    schema = SchemaState()
    clock = RecordingClock()
    started: list[list[StreamSink]] = []
    running = asyncio.Event()

    def on_start(sinks: Sequence[StreamSink]) -> None:
        started.append(list(sinks))
        running.set()

    config = SinkConfig(consumer="pod-a", streams=("calls",), batch_rows=1, batch_interval_s=0.01)
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
        await asyncio.sleep(0)
        assert connect.opened == 0
        schema.mark_applied()
        await asyncio.wait_for(running.wait(), timeout=5)
        (sink,) = started[0]
        assert sink.stream == "calls"
        await StreamConsumer(redis, "calls", GROUP, "pod-a").ensure_group()
        await redis.xadd("events:calls", {"payload": encode(turns(1)[0])})
        await asyncio.wait_for(sink.wait_flushes(1), timeout=5)
    finally:
        task.cancel()
        await asyncio.wait({task})
    assert (connect.opened, connect.closed, clock.sleeps[:1]) == (2, 1, [0.5])
    assert len(connect.client.rows_of("call_turns")) == 1


def test_the_default_config_matches_the_spec() -> None:
    config = SinkConfig(consumer="pod-x")
    assert (config.group, config.batch_rows, config.batch_interval_s) == ("events-sink", 1000, 1.0)
    assert config.streams == ("calls", "sms", "widget", "billing", "system")
    assert (config.initial_backoff_s, config.max_backoff_s, config.min_idle_ms) == (0.5, 5.0, 60_000)
