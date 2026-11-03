"""The call event stream over a real database and fakeredis (specs/19-events-and-webhooks.md §9 Decision
P06-T06a): replay order, the replay/tail boundary, gap recovery, ``Last-Event-ID``, heartbeats, and close."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Unpack

import fakeredis
import orjson
import pytest
from call_input_rows import make_ctx
from call_rows import seed_call
from clock_helpers import ManualClock
from live_stream_helpers import FrameReader, started
from service_helpers import json_log_stream, log_events
from tenant_rows import open_database

from va_api.services.call_stream import (
    CLOSE_AFTER_COMPLETED_S,
    GAP_HOLD_S,
    HEARTBEAT_S,
    MAX_STREAM_S,
    REPLAY_PAGE,
    CallStream,
    StreamDeps,
    open_call_stream,
)
from va_common.errors import AppError
from va_common.ids import new_id
from va_common.time import FrozenClock, SystemClock
from va_db.models import CallEvent
from va_db.session import Database
from va_events import (
    DomainEvent,
    EventFields,
    EventPublisher,
    EventType,
    LiveHub,
    insert_rows,
    iso_ms,
    new_event,
    row_for,
)
from va_events.keys import call_live_channel

pytestmark = [pytest.mark.integration, pytest.mark.timeout(240)]

METADATA: Final = {"crm_id": "42"}
WAIT_S: Final = 5.0


@dataclass(frozen=True, slots=True)
class Frame:
    id: int
    event: str | None
    data: dict[str, Any]


class Reader(FrameReader):
    """A :class:`FrameReader` that parses call stream frames."""

    async def frame(self) -> Frame:
        return parse(await self.raw())


def reading(stream: CallStream) -> AbstractAsyncContextManager[Reader]:
    return started(Reader(stream.frames()))


def parse(raw: bytes) -> Frame:
    assert raw.endswith(b"\n\n"), raw
    fields = dict(line.split(": ", 1) for line in raw.decode().strip("\n").split("\n"))
    return Frame(int(fields["id"]), fields.get("event"), orjson.loads(fields["data"]))


@dataclass
class Rig:
    database: Database
    redis: fakeredis.FakeAsyncRedis
    hub: LiveHub
    clock: ManualClock
    org_id: uuid.UUID
    call_id: uuid.UUID
    publisher: EventPublisher = field(init=False)

    def __post_init__(self) -> None:
        self.publisher = EventPublisher(self.redis, call_events=None)

    @property
    def deps(self) -> StreamDeps:
        return StreamDeps(database=self.database, live_hub=self.hub, clock=self.clock)

    def event(
        self, event_type: EventType = EventType.CALL_LATENCY, *, at: datetime | None = None, **fields: Any
    ) -> DomainEvent:
        values: EventFields = {"call_id": self.call_id, **fields}  # type: ignore[typeddict-item]
        return new_event(event_type, org_id=self.org_id, clock=FrozenClock(at or self.clock.now()), **values)

    async def sequenced(self, event_type: EventType = EventType.CALL_LATENCY, **fields: Any) -> DomainEvent:
        return await self.publisher.sequence(self.event(event_type, **fields))

    async def store(self, *events: DomainEvent) -> None:
        rows = [row for row in (row_for(event) for event in events) if row is not None]
        async with self.database.tenant_session(self.org_id) as session:
            await insert_rows(session, rows)

    async def stored(
        self, event_type: EventType = EventType.CALL_LATENCY, **fields: Unpack[EventFields]
    ) -> DomainEvent:
        event = await self.sequenced(event_type, **fields)
        await self.store(event)
        return event

    async def live(self, *events: DomainEvent) -> None:
        for event in events:
            await self.publisher.broadcast(event)

    async def open(self, last_event_id: str | None = None) -> CallStream:
        return await open_call_stream(self.deps, make_ctx(self.org_id), self.call_id, last_event_id)

    def subscribers(self) -> int:
        return self.hub.subscribers(call_live_channel(self.call_id))


@asynccontextmanager
async def stream_rig(migrated_url: str, *, queue_size: int = 1000) -> AsyncIterator[Rig]:
    redis = fakeredis.FakeAsyncRedis()
    hub = LiveHub(redis, SystemClock(), queue_size=queue_size)
    clock = ManualClock()
    clock.set(datetime.now(UTC))  # call_events partitions exist around the real current month
    async with open_database(migrated_url) as database:
        call = await seed_call(database, status="in_progress", metadata_=dict(METADATA))
        try:
            yield Rig(database, redis, hub, clock, call.org_id, call.call_id)
        finally:
            await hub.aclose()
            await redis.aclose()


@pytest.fixture
async def rig(migrated_url: str) -> AsyncIterator[Rig]:
    async with stream_rig(migrated_url) as opened:
        yield opened


async def test_replay_sends_stored_events_in_seq_order_with_bland_live_bodies(rig: Rig) -> None:
    first = await rig.stored(EventType.CALL_STARTED, message="Call started")
    second = await rig.stored(
        EventType.CALL_TURN, message="assistant", data={"text": "Hello", "turn_index": 0}
    )
    third = await rig.stored(EventType.CALL_LATENCY, message="E2E: 700ms", data={"e2e_ms": 700})
    async with reading(await rig.open()) as reader:
        frames = [await reader.frame() for _ in range(3)]
    assert [(f.id, f.event) for f in frames] == [(1, "call.started"), (2, "call.turn"), (3, "call.latency")]
    assert frames[1].data == {
        "call_id": str(rig.call_id),
        "category": "call",
        "level": "info",
        "message": "assistant",
        "data": {"text": "Hello", "turn_index": 0},
        "seq": 2,
        "created_at": iso_ms(second.occurred_at),
        "metadata": METADATA,
    }
    assert [first.seq, third.seq] == [1, 3]


async def test_a_row_without_event_type_has_no_event_line(rig: Rig) -> None:
    async with rig.database.tenant_session(rig.org_id) as session:
        session.add(
            CallEvent(
                id=new_id(),
                org_id=rig.org_id,
                call_id=rig.call_id,
                seq=1,
                category="call",
                message="before 0007",
                created_at=rig.clock.now(),
            )
        )
    async with reading(await rig.open()) as reader:
        raw = await reader.raw()
    assert raw.startswith(b"id: 1\ndata: {")
    assert parse(raw).event is None


async def test_replay_pages_through_more_than_one_page(rig: Rig) -> None:
    events = [await rig.sequenced(message=f"e{n}") for n in range(REPLAY_PAGE + 2)]
    await rig.store(*events)
    async with reading(await rig.open()) as reader:
        seqs = [(await reader.frame()).id for _ in events]
    assert seqs == list(range(1, REPLAY_PAGE + 3))


async def test_last_event_id_resumes_after_that_seq(rig: Rig) -> None:
    for _ in range(5):
        await rig.stored()
    async with reading(await rig.open(" 3 ")) as reader:
        assert [(await reader.frame()).id for _ in range(2)] == [4, 5]
        await rig.live(await rig.stored())
        assert (await reader.frame()).id == 6


async def test_events_on_both_sides_of_the_replay_boundary_are_sent_once(rig: Rig) -> None:
    replayed = [await rig.stored(), await rig.stored()]
    stream = await rig.open()
    # Published after the subscription and stored before the replay read: it is in both, sent once.
    boundary = await rig.stored()
    await rig.live(replayed[1], boundary)
    async with reading(stream) as reader:
        assert [(await reader.frame()).id for _ in range(3)] == [1, 2, 3]
        tail = await rig.stored()
        await rig.live(tail, boundary)
        assert (await reader.frame()).id == 4
        pending = reader.ask()
        await rig.clock.wait_for_sleep(HEARTBEAT_S)
        await rig.live(tail)
        await rig.clock.run_for(HEARTBEAT_S)
        assert await reader.raw() == b": ping\n\n"
        assert pending.done()


async def test_a_live_gap_is_filled_from_call_events_after_the_hold(rig: Rig) -> None:
    await rig.stored()
    # Published before the stream subscribed but not yet written by the media write-behind buffer.
    late = await rig.sequenced(message="late")
    async with reading(await rig.open()) as reader:
        assert (await reader.frame()).id == 1
        after = await rig.stored(message="after")
        await rig.live(after)
        pending = reader.ask()
        await rig.clock.wait_for_sleep(GAP_HOLD_S)
        assert not pending.done()
        await rig.store(late)
        await rig.clock.run_for(GAP_HOLD_S)
        frames = [await reader.frame(), await reader.frame()]
    assert [(f.id, f.data["message"]) for f in frames] == [(2, "late"), (3, "after")]


async def test_a_hole_that_never_arrives_is_skipped_after_the_hold(rig: Rig) -> None:
    await rig.stored()
    async with reading(await rig.open()) as reader:
        assert (await reader.frame()).id == 1
        lost = await rig.sequenced()
        after = await rig.stored()
        await rig.live(after)
        pending = reader.ask()
        await rig.clock.wait_for_sleep(GAP_HOLD_S)
        await rig.clock.run_for(GAP_HOLD_S)
        assert (await reader.frame()).id == after.seq
        assert pending.done()
        next_one = await rig.stored()
        await rig.live(next_one)
        assert (await reader.frame()).id == next_one.seq
    assert (lost.seq, after.seq) == (2, 3)


async def test_out_of_order_live_events_go_out_in_seq_order(rig: Rig) -> None:
    async with reading(await rig.open()) as reader:
        fifth, sixth = await rig.stored(), await rig.stored()
        await rig.live(sixth)
        await rig.live(fifth)
        assert [(await reader.frame()).id for _ in range(2)] == [fifth.seq, sixth.seq]


async def test_a_live_event_without_a_row_is_not_sent_but_fills_its_seq(rig: Rig) -> None:
    async with reading(await rig.open()) as reader:
        rowless = await rig.sequenced(EventType.BATCH_IN_PROGRESS)
        after = await rig.stored()
        await rig.live(after)
        await rig.live(rowless)
        frame = await reader.frame()
    assert (rowless.seq, frame.id) == (1, 2)


async def test_a_heartbeat_follows_fifteen_quiet_seconds(rig: Rig) -> None:
    async with reading(await rig.open()) as reader:
        reader.ask()
        await rig.clock.wait_for_sleep(HEARTBEAT_S)
        await rig.clock.run_for(HEARTBEAT_S)
        assert await reader.raw() == b": ping\n\n"


async def test_the_stream_ends_thirty_seconds_after_call_completed(rig: Rig) -> None:
    stream_log = json_log_stream()
    async with reading(await rig.open()) as reader:
        completed = await rig.stored(EventType.CALL_COMPLETED, data={"call_id": "x"})
        await rig.live(completed)
        frame = await reader.frame()
        assert (frame.id, frame.event) == (completed.seq, "call.completed")
        reader.ask()
        await rig.clock.wait_for_sleep(HEARTBEAT_S)
        await rig.clock.run_for(HEARTBEAT_S)
        assert await reader.raw() == b": ping\n\n"
        reader.ask()
        await rig.clock.wait_for_sleep(CLOSE_AFTER_COMPLETED_S - HEARTBEAT_S)
        await rig.clock.run_for(CLOSE_AFTER_COMPLETED_S - HEARTBEAT_S)
        assert await reader.ended()
    assert rig.subscribers() == 0
    (closed,) = [e for e in log_events(stream_log) if e["event"] == "calls.event_stream_closed"]
    assert (closed["reason"], closed["frames"], closed["call_id"]) == ("completed", 1, str(rig.call_id))


async def test_an_old_completed_call_ends_right_after_the_replay(rig: Rig) -> None:
    await rig.stored(EventType.CALL_STARTED)
    event = rig.event(EventType.CALL_COMPLETED, at=rig.clock.now() - timedelta(minutes=5))
    await rig.store(await rig.publisher.sequence(event))
    async with reading(await rig.open()) as reader:
        assert [(await reader.frame()).event for _ in range(2)] == ["call.started", "call.completed"]
        assert await reader.ended()


async def test_the_stream_ends_after_the_maximum_duration(rig: Rig) -> None:
    async with reading(await rig.open()) as reader:
        for _ in range(int(MAX_STREAM_S // HEARTBEAT_S) - 1):
            reader.ask()
            await rig.clock.wait_for_sleep(HEARTBEAT_S)
            await rig.clock.run_for(HEARTBEAT_S)
            assert await reader.raw() == b": ping\n\n"
        reader.ask()
        await rig.clock.wait_for_sleep(HEARTBEAT_S)
        await rig.clock.run_for(HEARTBEAT_S)
        assert await reader.ended()


async def test_an_overflowing_subscription_ends_the_stream(migrated_url: str) -> None:
    stream_log = json_log_stream()
    async with stream_rig(migrated_url, queue_size=2) as rig:
        stream = await rig.open()
        reader = Reader(stream.frames())
        events = [await rig.stored() for _ in range(3)]
        await rig.live(*events)
        # One connection reads in order: once the probe message arrived, the three events were dispatched.
        probe = await rig.hub.subscribe("probe")
        await rig.redis.publish("probe", b"after the events")
        async with asyncio.timeout(WAIT_S):
            assert await probe.next() == b"after the events"
        await reader.start()
        assert [(await reader.frame()).id for _ in range(3)] == [1, 2, 3]
        assert await reader.ended()
        await reader.aclose()
    (closed,) = [e for e in log_events(stream_log) if e["event"] == "calls.event_stream_closed"]
    assert closed["reason"] == "overflow"


async def test_a_closed_hub_ends_the_stream_and_refuses_new_ones(rig: Rig) -> None:
    async with reading(await rig.open()) as reader:
        await rig.hub.aclose()
        assert await reader.ended()
    with pytest.raises(AppError) as caught:
        await rig.open()
    assert caught.value.code == "SERVICE_UNAVAILABLE"


async def test_undecodable_and_foreign_live_messages_are_ignored(rig: Rig) -> None:
    async with reading(await rig.open()) as reader:
        channel = call_live_channel(rig.call_id)
        await rig.redis.publish(channel, b"not json")
        other = rig.event().model_copy(update={"call_id": uuid.uuid4(), "seq": 1})
        await rig.redis.publish(channel, other.model_dump_json())
        unsequenced = rig.event()
        await rig.redis.publish(channel, unsequenced.model_dump_json())
        event = await rig.stored()
        await rig.live(event)
        assert (await reader.frame()).id == event.seq


async def test_closing_the_stream_releases_the_subscription(rig: Rig) -> None:
    stream = await rig.open()
    assert rig.subscribers() == 1
    await stream.aclose()
    await stream.aclose()
    assert rig.subscribers() == 0


async def test_unknown_and_foreign_calls_are_not_found(rig: Rig) -> None:
    other = await seed_call(rig.database, status="in_progress")
    for org_id, call_id in (
        (rig.org_id, uuid.uuid4()),
        (rig.org_id, other.call_id),
        (other.org_id, rig.call_id),
    ):
        with pytest.raises(AppError) as caught:
            await open_call_stream(rig.deps, make_ctx(org_id), call_id, None)
        assert caught.value.code == "CALL_NOT_FOUND"
    assert rig.hub.subscribed_channels == frozenset()


@pytest.mark.parametrize("value", ["abc", "-1", "1.5", "٣"])
async def test_an_invalid_last_event_id_is_rejected(rig: Rig, value: str) -> None:
    with pytest.raises(AppError) as caught:
        await rig.open(value)
    assert (caught.value.code, caught.value.field) == ("INVALID_INPUT", "Last-Event-ID")
    assert rig.subscribers() == 0
