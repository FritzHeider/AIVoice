"""``TelnyxEventReprocessor`` (specs/06-telephony-telnyx.md §4.1 step 7, Decision P05-T01).

``updated_at`` comes from the database clock, so "30 s later" is a reprocessor clock set ahead of real time.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from clock_helpers import ManualClock
from edge_helpers import event_row, make_processor, running, stored_event, unreachable_database
from service_helpers import json_log_stream, log_events
from sqlalchemy import update

from va_common.time import FrozenClock
from va_db.models import TelnyxEvent
from va_db.repositories import TelnyxEventRepo
from va_db.session import Database
from va_edge.processor import StoredEvent
from va_edge.reprocessor import TelnyxEventReprocessor
from va_telnyx.webhooks import EventPayload

pytestmark = pytest.mark.integration


def _later(seconds: float) -> FrozenClock:
    return FrozenClock(datetime.now(UTC) + timedelta(seconds=seconds))


async def _fail(database: Database, event: StoredEvent, attempts: int = 1) -> None:
    async with database.system_session() as session:
        await TelnyxEventRepo(session).mark_processed(
            event.id, now=datetime.now(UTC), error="RuntimeError: x"
        )
        await session.execute(
            update(TelnyxEvent).where(TelnyxEvent.id == event.id).values(process_attempts=attempts)
        )


async def test_failed_event_is_retried_until_it_succeeds(database: Database) -> None:
    calls: list[int] = []
    event = await stored_event(database)

    async def flaky(seen: StoredEvent, _payload: EventPayload) -> None:
        # The claim may also pick up failed rows of other tests in the shared database.
        if seen.id != event.id:
            return
        calls.append(seen.attempts)
        if len(calls) == 1:
            raise RuntimeError

    processor = make_processor(database, {"call.answered": flaky})
    async with running(processor):
        processor.offer(event)
        await processor.join()
        assert (await event_row(database, event.id)).process_error == "RuntimeError: "
        await TelnyxEventReprocessor(database, processor, clock=_later(10)).run_once()
        await processor.join()
    row = await event_row(database, event.id)
    assert (row.process_error, row.process_attempts, calls) == (None, 2, [0, 1])


async def test_exhausted_event_is_left_alone(database: Database) -> None:
    exhausted, retried = await stored_event(database), await stored_event(database)
    await _fail(database, exhausted, attempts=5)
    await _fail(database, retried, attempts=4)
    processor = make_processor(database)
    async with running(processor):
        await TelnyxEventReprocessor(database, processor, clock=_later(10)).run_once()
        await processor.join()
    assert (await event_row(database, exhausted.id)).process_attempts == 5
    assert (await event_row(database, retried.id)).process_attempts == 5
    assert (await event_row(database, retried.id)).process_error is None


async def test_only_stale_unprocessed_events_are_reclaimed(database: Database) -> None:
    event = await stored_event(database)
    processor = make_processor(database)
    async with running(processor):
        await TelnyxEventReprocessor(database, processor, clock=_later(0)).run_once()
        await processor.join()
        assert (await event_row(database, event.id)).processed_at is None
        stream = json_log_stream()
        assert await TelnyxEventReprocessor(database, processor, clock=_later(60)).run_once() >= 1
        await processor.join()
    assert (await event_row(database, event.id)).process_attempts == 1
    [line] = [line for line in log_events(stream) if line["event"] == "telnyx.events_reclaimed"]
    assert line["claimed"] >= 1


async def test_run_loop_survives_database_failures() -> None:
    clock = ManualClock()
    database = unreachable_database()
    processor = make_processor(database)
    reprocessor = TelnyxEventReprocessor(database, processor, clock=clock, interval_s=5.0)
    stream = json_log_stream()
    task = asyncio.create_task(reprocessor.run())
    try:
        for _ in range(2):
            await clock.wait_for_sleep(5.0)
            await clock.run_for(5.0)
        await clock.wait_for_sleep(5.0)
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await database.dispose()
    failures = [line for line in log_events(stream) if line["event"] == "telnyx.reprocess_failed"]
    assert len(failures) == 2
