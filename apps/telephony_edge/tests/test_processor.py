"""Sharded event processing (specs/06-telephony-telnyx.md §4.1 step 7, Decision P05-T01)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from clock_helpers import ManualClock
from edge_helpers import (
    Keypair,
    edge_client,
    envelope,
    event_row,
    make_processor,
    post,
    running,
    stored_event,
    unreachable_database,
)
from service_helpers import json_log_stream, log_events

from va_db.session import Database
from va_edge.processor import EventProcessor, EventSettled, StoredEvent
from va_telnyx.webhooks import EventPayload

pytestmark = pytest.mark.integration


def _count(processor: EventProcessor, event_type: str, result: str) -> float:
    total = processor._total.labels(event_type=event_type, result=result)
    return float(total._value.get())


async def test_events_of_one_leg_are_processed_in_order(database: Database) -> None:
    seen: list[tuple[str, int]] = []

    async def handler(event: StoredEvent, _payload: EventPayload) -> None:
        await asyncio.sleep(0)
        seen.append((event.call_control_id or "", event.envelope["data"]["payload"]["seq"]))

    legs = ["v3:leg-a", "v3:leg-b", "v3:leg-c"]
    events = [
        await stored_event(database, envelope(payload={"call_control_id": leg, "seq": seq}))
        for seq in range(6)
        for leg in legs
    ]
    processor = make_processor(database, {"call.answered": handler}, shards=2)
    async with running(processor):
        assert all(processor.offer(event) for event in events)
        await processor.join()
    for leg in legs:
        assert [seq for ccid, seq in seen if ccid == leg] == list(range(6))
    assert len(seen) == 18


async def test_handler_failure_is_stored_and_counted(database: Database) -> None:
    async def failing(_event: StoredEvent, _payload: EventPayload) -> None:
        msg = "boom"
        raise RuntimeError(msg)

    event = await stored_event(database)
    stream = json_log_stream()
    processor = make_processor(database, {"call.answered": failing})
    async with running(processor):
        processor.offer(event)
        await processor.join()
    row = await event_row(database, event.id)
    assert (row.process_error, row.process_attempts) == ("RuntimeError: boom", 1)
    assert _count(processor, "call.answered", "error") == 1
    [failed] = [line for line in log_events(stream) if line["event"] == "telnyx.event_failed"]
    assert (failed["attempt"], failed["abandoned"], failed["error_type"]) == (1, False, "RuntimeError")
    assert "boom" not in str(failed)


async def test_settled_event_keeps_its_reason_without_a_retry(database: Database) -> None:
    async def settling(_event: StoredEvent, _payload: EventPayload) -> None:
        raise EventSettled("unknown_number")

    event = await stored_event(database)
    stream = json_log_stream()
    processor = make_processor(database, {"call.answered": settling}, max_attempts=4)
    async with running(processor):
        processor.offer(event)
        await processor.join()
    row = await event_row(database, event.id)
    assert (row.process_error, row.process_attempts) == ("unknown_number", 4)
    assert row.processed_at is not None
    assert _count(processor, "call.answered", "processed") == 1
    assert _count(processor, "call.answered", "error") == 0
    assert "telnyx.event_failed" not in [line["event"] for line in log_events(stream)]


async def test_last_attempt_is_logged_as_abandoned(database: Database) -> None:
    async def failing(_event: StoredEvent, _payload: EventPayload) -> None:
        raise ValueError

    event = await stored_event(database)
    stream = json_log_stream()
    processor = make_processor(database, {"call.answered": failing}, max_attempts=1)
    async with running(processor):
        processor.offer(event)
        await processor.join()
    [failed] = [line for line in log_events(stream) if line["event"] == "telnyx.event_failed"]
    assert failed["abandoned"] is True
    assert (await event_row(database, event.id)).process_error == "ValueError: "


async def test_handler_over_budget_is_cancelled(database: Database) -> None:
    cancelled = asyncio.Event()

    async def hanging(_event: StoredEvent, _payload: EventPayload) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    clock = ManualClock()
    event = await stored_event(database)
    processor = make_processor(database, {"call.answered": hanging}, clock=clock)
    async with running(processor):
        processor.offer(event)
        await clock.wait_for_sleep(2.0)
        await clock.run_for(2.0)
        await processor.join()
    assert cancelled.is_set()
    row = await event_row(database, event.id)
    assert row.process_error == "HandlerTimeoutError: the handler did not finish within 2.0 s"
    assert row.processed_at == clock.now()


async def test_full_shard_refuses_and_duplicates_are_queued_once(database: Database) -> None:
    first, second = await stored_event(database), await stored_event(database)
    processor = make_processor(database, shards=1, queue_size=1)
    stream = json_log_stream()
    assert processor.offer(first)
    assert processor.offer(first)
    assert not processor.offer(second)
    async with running(processor):
        await processor.join()
    assert (await event_row(database, first.id)).process_attempts == 1
    assert (await event_row(database, second.id)).processed_at is None
    assert [line["event"] for line in log_events(stream)] == ["telnyx.event_deferred"]
    # Once processed, the event may be offered again (a later reclaim).
    async with running(processor):
        assert processor.offer(first)
        await processor.join()
    assert (await event_row(database, first.id)).process_attempts == 2


async def test_mark_failure_keeps_the_worker_alive() -> None:
    database = unreachable_database()
    processor = make_processor(database)
    stream = json_log_stream()
    event = StoredEvent(
        id=uuid.uuid4(),
        telnyx_event_id="evt-1",
        event_type="call.hangup",
        occurred_at=datetime.now(UTC),
        envelope={
            "data": {"id": "evt-1", "event_type": "call.hangup", "occurred_at": "2026-11-01T00:00:00Z"}
        },
        org_id=None,
        call_id=None,
        call_control_id=None,
        attempts=0,
    )
    try:
        async with running(processor):
            processor.offer(event)
            await processor.join()
            processor.offer(event)
            await processor.join()
    finally:
        await database.dispose()
    marks = [line for line in log_events(stream) if line["event"] == "telnyx.event_mark_failed"]
    assert len(marks) == 2
    assert _count(processor, "call.hangup", "error") == 0


def test_invalid_sizes_are_rejected() -> None:
    database = unreachable_database()
    with pytest.raises(ValueError, match="at least 1"):
        make_processor(database, shards=0)
    with pytest.raises(ValueError, match="at least 1"):
        make_processor(database, queue_size=0)


async def test_intake_answers_503_once_shutting_down(edge_db_url: str) -> None:
    key = Keypair.generate()
    async with edge_client(edge_db_url, key) as (client, app):
        app.state.processor.close()
        response = await post(client, "/telnyx/voice", envelope(), key)
    assert (response.status_code, response.json()) == (503, {"error": "unavailable"})
