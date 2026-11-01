"""``CallEventBuffer`` flush triggers and failure handling (specs/19-events-and-webhooks.md §1 step 4)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from clock_helpers import ManualClock, settle
from event_helpers import ORG, RecordingWriter, WriterFailedError, event, publisher_rig, row
from service_helpers import json_log_stream, log_events

from va_events import CallEventBuffer, CallEventWriteError, EventType

CALL = uuid.UUID("0193c5d2-0000-7000-8000-0000000000aa")


@dataclass
class BufferRig:
    clock: ManualClock
    writer: RecordingWriter
    buffer: CallEventBuffer
    runner: asyncio.Task[None]


@pytest.fixture
async def rig() -> AsyncIterator[BufferRig]:
    clock = ManualClock()
    writer = RecordingWriter()
    buffer = CallEventBuffer(writer, clock=clock)
    runner = asyncio.create_task(buffer.run())
    await clock.wait_for_sleep(1.0)
    yield BufferRig(clock, writer, buffer, runner)
    await buffer.aclose()
    await asyncio.wait_for(runner, 5)


async def _add(buffer: CallEventBuffer, count: int, start: int = 1) -> None:
    for seq in range(start, start + count):
        await buffer.add(row(CALL, seq))


async def test_199_rows_wait_and_the_200th_flushes_one_batch(rig: BufferRig) -> None:
    await _add(rig.buffer, 199)
    await settle()
    assert rig.writer.batches == []
    assert rig.buffer.pending == 199
    await _add(rig.buffer, 1, start=200)
    await settle()
    assert [len(batch) for batch in rig.writer.batches] == [200]
    assert [r.seq for r in rig.writer.rows] == list(range(1, 201))
    assert rig.buffer.pending == 0


async def test_rows_are_flushed_every_second(rig: BufferRig) -> None:
    await _add(rig.buffer, 3)
    await rig.clock.run_for(0.5)
    assert rig.writer.batches == []
    await rig.clock.run_for(0.5)
    assert [len(batch) for batch in rig.writer.batches] == [3]
    await rig.clock.run_for(1.0)
    assert rig.writer.attempts == 1  # an empty interval writes nothing


async def test_call_end_flushes_immediately(rig: BufferRig) -> None:
    await _add(rig.buffer, 5)
    assert await rig.buffer.flush_call(CALL) == 5
    assert [len(batch) for batch in rig.writer.batches] == [5]
    assert await rig.buffer.flush_call(CALL) == 0


async def test_a_failed_flush_keeps_the_rows_in_order_for_the_next_trigger(rig: BufferRig) -> None:
    stream = json_log_stream()
    await _add(rig.buffer, 3)
    rig.writer.fail_next()
    await rig.clock.run_for(1.0)
    assert rig.writer.batches == []
    assert rig.buffer.pending == 3
    failures = [e["pending"] for e in log_events(stream) if e["event"] == "events.call_events_flush_failed"]
    assert failures == [3]
    await _add(rig.buffer, 2, start=4)
    await rig.clock.run_for(1.0)
    assert [r.seq for r in rig.writer.rows] == [1, 2, 3, 4, 5]
    assert not rig.runner.done()


async def test_flush_raises_and_requeues_on_writer_failure() -> None:
    writer = RecordingWriter()
    buffer = CallEventBuffer(writer, clock=ManualClock())
    await _add(buffer, 2)
    writer.fail_next()
    with pytest.raises(WriterFailedError):
        await buffer.flush_call(CALL)
    assert buffer.pending == 2
    assert await buffer.flush() == 2


async def test_a_failing_org_does_not_hold_back_the_others_and_only_its_rows_are_retried() -> None:
    org_b = uuid.UUID("0193c5d2-0000-7000-8000-0000000000b2")
    call_b = uuid.uuid4()
    writer = RecordingWriter(failing_orgs={org_b})
    buffer = CallEventBuffer(writer, clock=ManualClock())
    for seq in range(1, 4):
        await buffer.add(row(CALL, seq))
        await buffer.add(row(call_b, seq, org_id=org_b))
    with pytest.raises(CallEventWriteError) as raised:
        await buffer.flush()
    assert [(r.org_id, r.seq) for r in raised.value.unwritten] == [(org_b, 1), (org_b, 2), (org_b, 3)]
    assert [(r.org_id, r.seq) for r in writer.rows] == [(ORG, 1), (ORG, 2), (ORG, 3)]
    assert buffer.pending == 3

    await buffer.add(row(CALL, 4))
    writer.failing_orgs.clear()
    assert await buffer.flush() == 4
    assert [(r.org_id, r.seq) for r in writer.batches[-1]] == [(org_b, 1), (org_b, 2), (org_b, 3), (ORG, 4)]
    assert [r.seq for r in writer.rows if r.org_id == ORG] == [1, 2, 3, 4]  # org A never re-sent
    assert buffer.pending == 0


async def test_overflow_drops_the_oldest_rows_with_a_warning() -> None:
    stream = json_log_stream()
    writer = RecordingWriter()
    buffer = CallEventBuffer(writer, clock=ManualClock(), max_rows=5, max_pending=8)
    writer.fail_next(1)
    await _add(buffer, 6)
    with pytest.raises(WriterFailedError):
        await buffer.flush()
    await _add(buffer, 4, start=7)
    assert buffer.pending == 8
    await buffer.flush()
    assert [r.seq for r in writer.rows] == [3, 4, 5, 6, 7, 8, 9, 10]
    dropped = [e for e in log_events(stream) if e["event"] == "events.call_events_dropped"]
    assert sum(e["count"] for e in dropped) == 2
    assert all(e["reason"] == "overflow" for e in dropped)
    assert all("data" not in e and "message" not in e for e in dropped)


async def test_rows_failed_back_into_a_full_buffer_are_trimmed_oldest_first() -> None:
    writer = RecordingWriter()
    buffer = CallEventBuffer(writer, clock=ManualClock(), max_rows=2, max_pending=3)
    gate = asyncio.Event()

    async def slow_failing_write(rows: object) -> None:
        await gate.wait()
        raise WriterFailedError

    writer.write = slow_failing_write  # type: ignore[method-assign]
    await _add(buffer, 3)
    flushing = asyncio.create_task(buffer.flush())
    await settle()
    await _add(buffer, 2, start=4)
    gate.set()
    with pytest.raises(WriterFailedError):
        await flushing
    assert buffer.pending == 3
    del writer.write
    await buffer.flush()
    assert [r.seq for r in writer.rows] == [3, 4, 5]


async def test_aclose_flushes_what_is_pending_and_stops_the_loop(rig: BufferRig) -> None:
    await _add(rig.buffer, 7)
    await rig.buffer.aclose()
    await asyncio.wait_for(rig.runner, 5)
    assert [len(batch) for batch in rig.writer.batches] == [7]


async def test_rows_added_after_close_are_dropped() -> None:
    stream = json_log_stream()
    writer = RecordingWriter()
    buffer = CallEventBuffer(writer, clock=ManualClock())
    await buffer.aclose()
    await _add(buffer, 1)
    assert buffer.pending == 0
    assert [e["reason"] for e in log_events(stream) if e["event"] == "events.call_events_dropped"] == [
        "closed"
    ]


async def test_a_failed_final_flush_is_logged_and_keeps_the_rows() -> None:
    stream = json_log_stream()
    writer = RecordingWriter()
    buffer = CallEventBuffer(writer, clock=ManualClock())
    await _add(buffer, 2)
    writer.fail_next()
    await buffer.aclose()
    assert buffer.pending == 2
    assert any(e["event"] == "events.call_events_flush_failed" for e in log_events(stream))


async def test_concurrent_flushes_never_write_a_row_twice() -> None:
    writer = RecordingWriter()
    buffer = CallEventBuffer(writer, clock=ManualClock())
    await _add(buffer, 50)
    results = await asyncio.gather(*(buffer.flush() for _ in range(5)))
    assert sum(results) == 50
    assert sorted(r.seq for r in writer.rows) == list(range(1, 51))


def test_invalid_limits_are_rejected() -> None:
    with pytest.raises(ValueError, match="max_rows"):
        CallEventBuffer(RecordingWriter(), clock=ManualClock(), max_rows=0)
    with pytest.raises(ValueError, match="max_rows"):
        CallEventBuffer(RecordingWriter(), clock=ManualClock(), max_rows=20, max_pending=10)


async def test_the_publisher_feeds_the_buffer() -> None:
    writer = RecordingWriter()
    buffer = CallEventBuffer(writer, clock=ManualClock())
    rig = publisher_rig(buffer=buffer)
    for _ in range(3):
        await rig.publisher.publish(event(EventType.CALL_TURN, call_id=CALL))
    assert buffer.pending == 3
    await buffer.flush_call(CALL)
    assert [r.seq for r in writer.rows] == [1, 2, 3]
