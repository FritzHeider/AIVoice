"""``TurnWriter`` against a migrated Postgres: interval flush on the injected clock, per-call flush, per-org
transactions with in-order retry of a failed org, close, and the pending cap (specs/07 §8 Decision
P05-T11a)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from call_rows import SeededCall, seed_call
from clock_helpers import ManualClock
from service_helpers import json_log_stream, log_events
from tenant_rows import open_database

from va_db.call_turns import CallTurnRow, LatencyMerge, TurnDelete
from va_db.models import CallTurn
from va_db.repositories import CallTurnRepo
from va_db.session import Database
from va_media.turn_writer import TURN_FLUSH_INTERVAL_S, TurnWriteError, TurnWriter

pytestmark = pytest.mark.integration


@pytest.fixture
async def database(media_db_url: str) -> AsyncIterator[Database]:
    async with open_database(media_db_url) as db:
        yield db


@dataclass
class WriterRig:
    clock: ManualClock
    writer: TurnWriter
    runner: asyncio.Task[None]


@pytest.fixture
async def rig(database: Database) -> AsyncIterator[WriterRig]:
    clock = ManualClock()
    writer = TurnWriter(database, clock=clock)
    runner = asyncio.create_task(writer.run())
    await clock.wait_for_sleep(TURN_FLUSH_INTERVAL_S)
    yield WriterRig(clock, writer, runner)
    await writer.aclose()
    await runner


def user_row(call: SeededCall, index: int, text: str = "hello", **latency: int) -> CallTurnRow:
    return CallTurnRow(
        org_id=call.org_id,
        call_id=call.call_id,
        turn_index=index,
        role="user",
        text=text,
        start_ms=index * 1000,
        end_ms=index * 1000 + 400,
        latency=dict(latency),
    )


async def stored(database: Database, call: SeededCall) -> list[CallTurn]:
    async with database.tenant_session(call.org_id) as session:
        return await CallTurnRepo(session, call.org_id).list_for_call(call.call_id)


async def test_pending_ops_are_written_every_interval(database: Database, rig: WriterRig) -> None:
    call = await seed_call(database, status="in_progress")
    rig.writer.add(user_row(call, 1))
    await rig.clock.run_for(TURN_FLUSH_INTERVAL_S / 2)
    assert rig.writer.pending == 1
    assert await stored(database, call) == []
    await rig.clock.run_for(TURN_FLUSH_INTERVAL_S / 2)
    await rig.clock.wait_for_sleep(TURN_FLUSH_INTERVAL_S)
    assert rig.writer.pending == 0
    assert [t.text_ for t in await stored(database, call)] == ["hello"]


async def test_ops_of_one_call_apply_in_order(database: Database, rig: WriterRig) -> None:
    call = await seed_call(database, status="in_progress")
    writer = rig.writer
    writer.add(user_row(call, 1, "book"))
    writer.add(LatencyMerge(call.org_id, call.call_id, 1, "user", {"e2e_ms": 900}))
    writer.add(TurnDelete(call.org_id, call.call_id, 1, "user"))
    writer.add(user_row(call, 2, "book a table", stt_final_ms=60))
    writer.add(LatencyMerge(call.org_id, call.call_id, 2, "user", {"e2e_ms": 700}))
    writer.add(LatencyMerge(call.org_id, call.call_id, 7, "user", {"e2e_ms": 1}))
    assert await writer.flush() == 6
    turns = await stored(database, call)
    assert [(t.turn_index, t.text_, t.latency) for t in turns] == [
        (2, "book a table", {"stt_final_ms": 60, "e2e_ms": 700})
    ]


async def test_flush_call_writes_only_that_call(database: Database, rig: WriterRig) -> None:
    first = await seed_call(database, status="in_progress")
    second = await seed_call(database, status="in_progress")
    rig.writer.add(user_row(first, 1))
    rig.writer.add(user_row(second, 1))
    rig.writer.add(user_row(first, 3))
    assert await rig.writer.flush_call(first.call_id) == 2
    assert await rig.writer.flush_call(uuid.uuid4()) == 0
    assert rig.writer.pending == 1
    assert [t.turn_index for t in await stored(database, first)] == [1, 3]
    assert await stored(database, second) == []


async def test_a_failed_org_is_requeued_in_order_and_the_others_are_written(
    database: Database, rig: WriterRig
) -> None:
    good = await seed_call(database, status="in_progress")
    bad = await seed_call(database, status="in_progress")
    missing_call = SeededCall(org_id=bad.org_id, call_id=uuid.uuid4())
    stream = json_log_stream()
    rig.writer.add(user_row(bad, 1, "kept first"))
    rig.writer.add(user_row(good, 1))
    rig.writer.add(user_row(missing_call, 2))
    with pytest.raises(TurnWriteError) as caught:
        await rig.writer.flush()
    assert (caught.value.unwritten, caught.value.failed_orgs) == (2, 1)
    assert caught.value.__cause__ is not None
    assert len(await stored(database, good)) == 1
    assert await stored(database, bad) == []
    assert rig.writer.pending == 2
    failures = [e for e in log_events(stream) if e["event"] == "media.turns_org_write_failed"]
    assert [(e["org_id"], e["ops"]) for e in failures] == [(str(bad.org_id), 2)]
    assert "kept first" not in stream.getvalue()
    # The failing op is dropped from the queue by flushing the bad org's real call only.
    assert await rig.writer.flush_call(bad.call_id) == 1
    assert [t.text_ for t in await stored(database, bad)] == ["kept first"]


async def test_the_loop_survives_a_failed_flush(database: Database, rig: WriterRig) -> None:
    call = await seed_call(database, status="in_progress")
    stream = json_log_stream()
    rig.writer.add(user_row(SeededCall(org_id=call.org_id, call_id=uuid.uuid4()), 1))
    await rig.clock.run_for(TURN_FLUSH_INTERVAL_S)
    await rig.clock.wait_for_sleep(TURN_FLUSH_INTERVAL_S)
    assert not rig.runner.done()
    assert rig.writer.pending == 1
    warnings = [e for e in log_events(stream) if e["event"] == "media.turns_flush_failed"]
    assert [(e["error"], e["pending"]) for e in warnings] == [("TurnWriteError", 1)]
    rig.writer.add(user_row(call, 2))
    assert await rig.writer.flush_call(call.call_id) == 1


async def test_close_writes_pending_ops_and_drops_later_ones(database: Database) -> None:
    call = await seed_call(database, status="in_progress")
    clock = ManualClock()
    writer = TurnWriter(database, clock=clock)
    runner = asyncio.create_task(writer.run())
    await clock.wait_for_sleep(TURN_FLUSH_INTERVAL_S)
    writer.add(user_row(call, 1))
    stream = json_log_stream()
    await writer.aclose()
    await runner
    writer.add(user_row(call, 2))
    assert writer.pending == 0
    assert [t.turn_index for t in await stored(database, call)] == [1]
    dropped = [e for e in log_events(stream) if e["event"] == "media.turns_dropped"]
    assert [(e["count"], e["reason"]) for e in dropped] == [(1, "closed")]


async def test_run_returns_at_once_when_already_closed(database: Database) -> None:
    writer = TurnWriter(database, clock=ManualClock())
    await writer.aclose()
    await writer.run()
    assert await writer.flush() == 0


async def test_the_pending_cap_drops_the_oldest(database: Database) -> None:
    call = await seed_call(database, status="in_progress")
    writer = TurnWriter(database, clock=ManualClock(), max_pending=2)
    stream = json_log_stream()
    for index in range(3):
        writer.add(user_row(call, index))
    assert writer.pending == 2
    dropped = [e for e in log_events(stream) if e["event"] == "media.turns_dropped"]
    assert [(e["count"], e["reason"]) for e in dropped] == [(1, "overflow")]
    assert await writer.flush() == 2
    assert [t.turn_index for t in await stored(database, call)] == [1, 2]


def test_max_pending_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_pending"):
        TurnWriter(Database.__new__(Database), clock=ManualClock(), max_pending=0)
