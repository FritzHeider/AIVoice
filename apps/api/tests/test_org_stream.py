"""The org event stream over a real database and fakeredis (specs/19-events-and-webhooks.md §9 Decision
P06-T06c): ``types`` parsing, webhook-identical bodies, org and type filtering, internal types, HIPAA
stripping, heartbeats, and how the stream ends."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Unpack

import fakeredis
import orjson
import pytest
from call_input_rows import make_ctx
from call_rows import seed_call
from clock_helpers import ManualClock
from live_stream_helpers import FrameReader, started
from service_helpers import json_log_stream, log_events
from sqlalchemy.exc import OperationalError
from tenant_rows import RowFactory, open_database

from va_api.services import org_stream
from va_api.services.org_stream import (
    HEARTBEAT_S,
    MAX_PATTERNS,
    MAX_STREAM_S,
    TYPES_MESSAGE,
    MetadataCache,
    OrgStream,
    OrgStreamDeps,
    open_org_stream,
    parse_types,
)
from va_common.errors import AppError
from va_db.base import Base
from va_db.session import Database
from va_events import (
    DomainEvent,
    EventFields,
    EventPublisher,
    EventType,
    StreamTailHub,
    new_event,
    webhook_router,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(240)]

METADATA: Final = {"crm_id": "42"}


@dataclass(frozen=True, slots=True)
class Frame:
    id: str
    event: str
    data: dict[str, Any]


class Reader(FrameReader):
    async def frame(self) -> Frame:
        raw = await self.raw()
        fields = dict(line.split(": ", 1) for line in raw.decode().strip("\n").split("\n"))
        return Frame(fields["id"], fields["event"], orjson.loads(fields["data"]))


def reading(stream: OrgStream) -> AbstractAsyncContextManager[Reader]:
    return started(Reader(stream.frames()))


@dataclass(frozen=True, slots=True)
class StaticSettings:
    """An ``OrgSettingsReader`` stand-in answering one ``hipaa_mode`` for every org."""

    hipaa_mode: bool = False

    async def get(self, org_id: uuid.UUID) -> StaticSettings:
        assert isinstance(org_id, uuid.UUID)
        return self


@dataclass
class Rig:
    database: Database
    redis: fakeredis.FakeAsyncRedis
    hub: StreamTailHub
    clock: ManualClock
    org_id: uuid.UUID
    call_id: uuid.UUID
    settings: StaticSettings = field(default_factory=StaticSettings)

    def deps(self) -> OrgStreamDeps:
        return OrgStreamDeps(
            database=self.database,
            stream_tail=self.hub,
            org_settings=self.settings,  # type: ignore[arg-type]
            clock=self.clock,
        )

    async def open(self, types: str | None = None, *, org_id: uuid.UUID | None = None) -> OrgStream:
        return await open_org_stream(self.deps(), make_ctx(org_id or self.org_id), types)

    async def publish(
        self, event_type: EventType, *, org_id: uuid.UUID | None = None, **fields: Unpack[EventFields]
    ) -> DomainEvent:
        event = new_event(event_type, org_id=org_id or self.org_id, clock=self.clock, **fields)
        await EventPublisher(self.redis, call_events=None).broadcast(event)
        return event

    async def turn(self, text: str = "Hello") -> DomainEvent:
        return await self.publish(
            EventType.CALL_TURN, call_id=self.call_id, message="user", data={"text": text, "turn_index": 0}
        )


@asynccontextmanager
async def org_rig(migrated_url: str, *, queue_size: int = 1_000) -> AsyncIterator[Rig]:
    async with open_database(migrated_url) as database:
        call = await seed_call(database, status="in_progress", metadata_=dict(METADATA))
        clock = ManualClock()
        clock.set(datetime.now(UTC))  # the rows' and events' dates fall inside the call_events partitions
        redis = fakeredis.FakeAsyncRedis()
        rig = Rig(
            database, redis, StreamTailHub(redis, queue_size=queue_size), clock, call.org_id, call.call_id
        )
        try:
            yield rig
        finally:
            await rig.hub.aclose()
            await redis.aclose()


@pytest.fixture
async def rig(migrated_url: str) -> AsyncIterator[Rig]:
    async with org_rig(migrated_url) as opened:
        yield opened


def test_types_default_to_every_type_and_ignore_blank_parts() -> None:
    assert parse_types(None) == ("*",)
    assert parse_types("") == ("*",)
    assert parse_types(" , ,") == ("*",)
    assert parse_types(" call.* , sms.received,,") == ("call.*", "sms.received")


@pytest.mark.parametrize(
    "value",
    ["Call.*", "call.turn;drop", "call turn", "x" * 65, ",".join(["call.*"] * (MAX_PATTERNS + 1))],
)
def test_malformed_types_are_invalid_input(value: str) -> None:
    with pytest.raises(AppError) as raised:
        parse_types(value)
    assert (raised.value.code, raised.value.field, raised.value.message) == (
        "INVALID_INPUT",
        "types",
        TYPES_MESSAGE,
    )


def test_the_metadata_cache_drops_the_least_recently_used_call() -> None:
    cache = MetadataCache(size=2)
    first, second, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cache.put(first, {"n": 1})
    cache.put(second, {"n": 2})
    assert cache.get(first) == {"n": 1}
    cache.put(third, {"n": 3})
    assert cache.get(second) is None
    assert cache.get(first) == {"n": 1}
    assert cache.get(third) == {"n": 3}


async def test_a_call_event_is_framed_as_the_webhook_envelope_with_its_metadata(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[uuid.UUID] = []
    original = webhook_router.call_metadata

    async def counted(session: Any, org_id: uuid.UUID, call_id: uuid.UUID) -> Any:
        reads.append(call_id)
        return await original(session, org_id, call_id)

    monkeypatch.setattr(org_stream, "call_metadata", counted)
    async with reading(await rig.open()) as reader:
        first = await rig.turn("Hello")
        second = await rig.turn("Again")
        frames = [await reader.frame(), await reader.frame()]
    assert [(frame.id, frame.event) for frame in frames] == [
        (first.id, "call.turn"),
        (second.id, "call.turn"),
    ]
    envelope = frames[0].data
    assert envelope["id"] == first.id
    assert envelope["type"] == "call.turn"
    assert envelope["org_id"] == str(rig.org_id)
    assert envelope["call_id"] == str(rig.call_id)
    assert envelope["batch_id"] is None
    assert envelope["data"]["metadata"] == METADATA
    assert envelope["data"]["data"]["text"] == "Hello"
    assert reads == [rig.call_id]


async def test_call_completed_carries_its_call_object_and_reads_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbidden(*_: Any) -> Any:
        raise AssertionError

    monkeypatch.setattr(org_stream, "call_metadata", forbidden)
    call_object: dict[str, Any] = {"call_id": str(rig.call_id), "transcripts": [{"text": "hi"}]}
    async with reading(await rig.open()) as reader:
        completed = await rig.publish(EventType.CALL_COMPLETED, call_id=rig.call_id, data=call_object)
        frame = await reader.frame()
    assert frame.id == completed.id
    assert frame.data["data"] == call_object


async def test_batch_events_use_the_batch_row_and_other_events_their_data(rig: Rig) -> None:
    async with rig.database.tenant_session(rig.org_id) as session:
        key = await RowFactory().insert(session, Base.metadata.tables["batches"], rig.org_id)
    batch_id = key["id"]
    async with reading(await rig.open()) as reader:
        await rig.publish(EventType.BATCH_IN_PROGRESS, batch_id=batch_id, data={"ignored": True})
        await rig.publish(EventType.BATCH_IN_PROGRESS, batch_id=uuid.uuid4(), data={"raw": 1})
        await rig.publish(EventType.SMS_RECEIVED, data={"body": "yes"})
        known, unknown, sms = [(await reader.frame()).data for _ in range(3)]
    assert known["batch_id"] == str(batch_id)
    assert known["data"]["batch_id"] == str(batch_id)
    assert known["data"]["phase"] == "batch.in_progress"
    assert unknown["data"] == {"raw": 1}
    assert sms["data"] == {"body": "yes"}


async def test_an_unknown_call_gets_empty_metadata(rig: Rig) -> None:
    async with reading(await rig.open()) as reader:
        await rig.publish(EventType.CALL_LATENCY, call_id=uuid.uuid4(), data={"e2e_ms": 700})
        frame = await reader.frame()
    assert frame.data["data"]["metadata"] == {}


async def test_only_the_orgs_wanted_events_are_sent(rig: Rig) -> None:
    other = await seed_call(rig.database, status="in_progress")
    async with reading(await rig.open("call.*")) as reader:
        await rig.publish(EventType.CALL_LATENCY, org_id=other.org_id, call_id=other.call_id)
        await rig.publish(EventType.SMS_RECEIVED, data={"body": "no"})
        await rig.publish(EventType.BILLING_USAGE, data={"micros": 5})
        wanted = await rig.turn()
        assert (await reader.frame()).id == wanted.id


async def test_internal_types_are_never_sent_even_for_every_type(rig: Rig) -> None:
    async with reading(await rig.open("*,billing.*")) as reader:
        await rig.publish(EventType.BILLING_USAGE, data={"micros": 5})
        low = await rig.publish(EventType.BILLING_LOW_BALANCE, data={"balance": "1.00"})
        assert (await reader.frame()).id == low.id


async def test_a_hipaa_org_gets_bodies_without_transcript_text(rig: Rig) -> None:
    rig.settings = StaticSettings(hipaa_mode=True)
    async with reading(await rig.open()) as reader:
        await rig.turn("secret")
        await rig.publish(
            EventType.CALL_COMPLETED,
            call_id=rig.call_id,
            data={"transcripts": [{"text": "secret"}], "concatenated_transcript": "secret"},
        )
        turn, completed = (await reader.frame()).data, (await reader.frame()).data
    assert "text" not in turn["data"]["data"]
    assert completed["data"]["transcripts"] == []
    assert completed["data"]["concatenated_transcript"] == ""


async def test_an_event_whose_facts_cannot_be_read_is_skipped(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream_log = json_log_stream()

    async def broken(*_: Any) -> Any:
        raise OperationalError("SELECT", {}, OSError("connection lost"))

    monkeypatch.setattr(org_stream, "call_metadata", broken)
    async with reading(await rig.open()) as reader:
        await rig.turn()
        sms = await rig.publish(EventType.SMS_RECEIVED, data={"body": "ok"})
        assert (await reader.frame()).id == sms.id
    failed = [
        entry for entry in log_events(stream_log) if entry["event"] == "events.org_stream_render_failed"
    ]
    assert [entry["event_type"] for entry in failed] == ["call.turn"]


async def test_a_heartbeat_follows_fifteen_quiet_seconds_and_the_stream_ends_after_four_hours(
    rig: Rig,
) -> None:
    stream_log = json_log_stream()
    stream = await rig.open()
    async with reading(stream) as reader:
        for quiet_s in (HEARTBEAT_S, MAX_STREAM_S):
            next_frame = reader.ask()
            await rig.clock.wait_for_sleep(HEARTBEAT_S)
            await rig.clock.run_for(quiet_s)
            assert await reader.raw() == b": ping\n\n"
            assert next_frame.done()
        assert await reader.ended()
    closed = [entry for entry in log_events(stream_log) if entry["event"] == "events.org_stream_closed"]
    assert [(entry["reason"], entry["frames"]) for entry in closed] == [("max_duration", 0)]
    assert rig.hub.subscribers() == 0


async def test_an_overflowing_subscription_ends_the_stream(migrated_url: str) -> None:
    stream_log = json_log_stream()
    async with org_rig(migrated_url, queue_size=2) as rig:
        stream = await rig.open()
        probe_org = uuid.uuid4()
        probe = await rig.hub.subscribe(probe_org, lambda _: True)
        for _ in range(3):
            await rig.turn()
        # One reader dispatches in stream order: once the probe arrived, the three turns were dispatched.
        marker = await rig.publish(EventType.SMS_RECEIVED, org_id=probe_org, data={})
        async with asyncio.timeout(5):
            assert (await probe.next()).id == marker.id
        async with reading(stream) as reader:
            assert await reader.ended()
    closed = [entry for entry in log_events(stream_log) if entry["event"] == "events.org_stream_closed"]
    assert [entry["reason"] for entry in closed] == ["overflow"]


async def test_a_closed_hub_is_service_unavailable(rig: Rig) -> None:
    await rig.hub.aclose()
    with pytest.raises(AppError) as raised:
        await rig.open()
    assert raised.value.code == "SERVICE_UNAVAILABLE"


async def test_bad_types_are_refused_before_subscribing(rig: Rig) -> None:
    with pytest.raises(AppError) as raised:
        await rig.open("BAD!")
    assert raised.value.code == "INVALID_INPUT"
    assert rig.hub.subscribers() == 0
