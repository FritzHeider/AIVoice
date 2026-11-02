"""The media end-of-call routine on a real database (specs/07 §18, §19; specs/07 §18 Decision P05-T11b)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import fakeredis
import pytest
from call_rows import SeededCall, load_call, seed_call, seed_leg
from clock_helpers import ManualClock
from media_helpers import FakeSocket, RecordingSignaller, call_context
from service_helpers import json_log_stream, log_events
from sqlalchemy.exc import OperationalError
from tenant_rows import open_database

from va_billing.keys import concurrency_key
from va_common.metrics import Service, build_metrics, new_registry
from va_db.models import CallStatus
from va_db.session import Database
from va_domain.call_workflow import PipelineFinished
from va_domain.carrier import CarrierEvent, carrier_state_key
from va_events import DomainEvent, EventPublishError, EventType
from va_media.call_end import STREAM_CLOSED_WAIT_S, STREAM_LOST_MESSAGE, CallEnd
from va_media.channels import CallFeed
from va_media.ownership import CallOwnership
from va_media.session import OWNERSHIP_LOST, STREAM_CLOSED, CallSession, SessionEnd, StreamStart
from va_telnyx import TelnyxClientFactory, UnknownTelnyxAccount
from va_telnyx.client_state import command_id, decode
from va_telnyx.errors import TelnyxTransportError
from va_telnyx.media import MediaFormat

pytestmark = pytest.mark.integration

CONTROL_ID = "v3:cc-end"
START = StreamStart(
    stream_id="stream-end",
    call_control_id=CONTROL_ID,
    media_format=MediaFormat(encoding="PCMU", sample_rate=8000, channels=1),
)


@dataclass
class FakeTelnyx:
    """A Telnyx client factory + client: records hangups (``fail``: raise instead)."""

    fail: Exception | None = None
    already_ended: bool = False
    account_failure: Exception | None = None
    hangups: list[tuple[str, str]] = field(default_factory=list)
    stream_stops: list[tuple[str, str]] = field(default_factory=list)
    accounts: list[uuid.UUID | None] = field(default_factory=list)

    async def for_account(self, account_id: uuid.UUID | None) -> FakeTelnyx:
        self.accounts.append(account_id)
        if self.account_failure is not None:
            raise self.account_failure
        return self

    async def hangup(self, call_control_id: str, *, command_id: str, client_state: str | None = None) -> bool:
        assert client_state
        if self.fail is not None:
            raise self.fail
        self.hangups.append((call_control_id, command_id))
        return not self.already_ended

    async def streaming_stop(self, call_control_id: str, *, command_id: str, client_state: str) -> object:
        assert decode(client_state).leg == "primary"
        if self.fail is not None:
            raise self.fail
        self.stream_stops.append((call_control_id, command_id))
        return None


@dataclass
class FakeEvents:
    fail: bool = False
    published: list[DomainEvent] = field(default_factory=list)
    flushed: list[uuid.UUID] = field(default_factory=list)

    async def publish(self, event: DomainEvent) -> DomainEvent:
        if self.fail:
            raise EventPublishError(event, "seq")
        self.published.append(event)
        return event

    async def flush_call(self, call_id: uuid.UUID) -> int:
        if self.fail:
            msg = "rows down"
            raise OSError(msg)
        self.flushed.append(call_id)
        return 1


class BrokenDatabase:
    """A database whose tenant sessions fail to open."""

    def tenant_session(self, _org_id: uuid.UUID) -> BrokenDatabase:
        return self

    async def __aenter__(self) -> None:
        raise OperationalError("SELECT", None, ConnectionError("down"))

    async def __aexit__(self, *_exc: object) -> None:
        return None


@dataclass
class Rig:
    database: Database
    redis: fakeredis.FakeAsyncRedis
    clock: ManualClock
    telnyx: FakeTelnyx
    events: FakeEvents
    signaller: RecordingSignaller
    call: SeededCall
    session: CallSession
    ender: CallEnd

    async def finish(self, end: SessionEnd | None) -> None:
        await self.ender.finish(self.session, end)

    def signalled(self) -> list[tuple[str, str]]:
        return [(f.end_reason, f.ended_by) for _, f in self.signaller.signals]

    async def slot_held(self) -> bool:
        return await self.redis.zscore(concurrency_key(self.call.org_id), str(self.call.call_id)) is not None

    def ended_events(self) -> list[dict[str, Any]]:
        return [dict(e.data) for e in self.events.published if e.type is EventType.CALL_ENDED]


@pytest.fixture
async def database(media_db_url: str) -> AsyncIterator[Database]:
    async with open_database(media_db_url) as db:
        yield db


@asynccontextmanager
async def rig(  # noqa: PLR0913 - keyword-only test knobs
    database: Database,
    *,
    status: CallStatus = "in_progress",
    telnyx: FakeTelnyx | None = None,
    events: FakeEvents | None = None,
    state: dict[str, str] | None = None,
    broken_db: bool = False,
    no_telnyx: bool = False,
    **values: Any,
) -> AsyncIterator[Rig]:
    clock = ManualClock()
    columns: dict[str, Any] = {"answered_at": clock.now() - timedelta(seconds=60), **values}
    call = await seed_call(database, status=status, **columns)
    redis = fakeredis.FakeAsyncRedis()
    await redis.zadd(concurrency_key(call.org_id), {str(call.call_id): 1})
    if state:
        for name, value in state.items():
            await redis.hset(carrier_state_key(call.call_id), name, value)
    fake_telnyx = telnyx if telnyx is not None else FakeTelnyx()
    fake_events = events if events is not None else FakeEvents()
    signaller = RecordingSignaller()
    session = CallSession(
        context=call_context(call.call_id, org_id=call.org_id),
        socket=FakeSocket(),
        start=START,
        ownership=CallOwnership(redis, call.call_id, "pod-test"),
        redis=redis,
        clock=clock,
        metrics=build_metrics(Service.MEDIA_WORKER, new_registry(process_collectors=False)),
        feed=CallFeed(call.call_id),
    )
    ender = CallEnd(
        database=cast("Database", BrokenDatabase()) if broken_db else database,
        redis=redis,
        signaller=signaller,
        clock=clock,
        telnyx=None if no_telnyx else cast("TelnyxClientFactory", fake_telnyx),
        events=fake_events,
        event_rows=fake_events,
    )
    yield Rig(database, redis, clock, fake_telnyx, fake_events, signaller, call, session, ender)


def ended(reason: str, ended_by: str) -> SessionEnd:
    return SessionEnd("ended", end_reason=reason, ended_by=ended_by)


def hangup_command(r: Rig) -> list[tuple[str, str]]:
    return [(CONTROL_ID, command_id("hangup", r.call.call_id, "primary"))]


@pytest.mark.parametrize(
    ("reason", "ended_by"),
    [
        ("agent_end_call", "agent"),
        ("no_input_timeout", "system"),
        ("max_duration", "timeout"),
        ("ivr_dead_end", "system"),
        ("error", "error"),
        ("api_stop", "api"),
        ("insufficient_credits", "system"),
    ],
)
async def test_a_pipeline_end_is_recorded_hung_up_announced_and_signalled(
    database: Database, reason: str, ended_by: str
) -> None:
    async with rig(database) as r:
        await r.finish(ended(reason, ended_by))
        call = await load_call(database, r.call)
        assert (call.status, call.end_reason, call.ended_by) == ("completed", reason, ended_by)
        assert call.ended_at == r.clock.now()
        assert call.connected_seconds == Decimal(60)
        assert r.telnyx.hangups == hangup_command(r)
        assert r.ended_events() == [{"end_reason": reason, "ended_by": ended_by}]
        assert r.events.flushed == [r.call.call_id]
        assert not await r.slot_held()
        assert r.signaller.signals == [
            (r.call.call_id, PipelineFinished(end_reason=reason, ended_by=ended_by))
        ]


async def test_a_stream_closed_after_a_stored_carrier_hangup_records_user_hangup(database: Database) -> None:
    async with rig(database, state={"answered": "1", "primary_hangup": "1", "hangup_cause": "normal"}) as r:
        leg_ended_at = r.clock.now() - timedelta(seconds=2)
        await seed_leg(database, r.call, CONTROL_ID, state="hangup", ended_at=leg_ended_at)
        await r.finish(STREAM_CLOSED)
        call = await load_call(database, r.call)
        assert (call.end_reason, call.ended_by, call.ended_at) == ("user_hangup", "user", leg_ended_at)
        assert call.connected_seconds == Decimal(58)
        assert r.telnyx.hangups == []
        assert r.signalled() == [("user_hangup", "user")]
        assert not await r.slot_held()


async def test_a_carrier_hangup_of_a_transferred_call_records_transferred(database: Database) -> None:
    transfer_at = datetime.now().astimezone() - timedelta(days=1)
    async with rig(
        database,
        status="transferred",
        state={"primary_hangup": "1", "transfer_started_at": transfer_at.isoformat()},
    ) as r:
        await r.finish(STREAM_CLOSED)
        call = await load_call(database, r.call)
        assert (call.status, call.end_reason) == ("completed", "transferred")
        assert call.transfer_seconds is not None
        assert r.telnyx.hangups == []


async def test_a_bridged_transfer_stops_the_stream_and_leaves_the_call_up(database: Database) -> None:
    async with rig(database, status="transferred") as r:
        await r.finish(ended("transferred", "system"))
        call = await load_call(database, r.call)
        assert (call.status, call.end_reason) == ("transferred", None)
        assert r.telnyx.stream_stops == [
            (CONTROL_ID, command_id("streaming_stop", r.call.call_id, "primary"))
        ]
        assert r.telnyx.hangups == []
        assert r.ended_events() == []
        assert r.events.flushed == [r.call.call_id]
        assert await r.slot_held()
        assert r.signalled() == [("transferred", "system")]


@pytest.mark.parametrize("no_telnyx", [False, True], ids=["fails", "no_factory"])
async def test_a_failed_streaming_stop_is_logged_and_still_signalled(
    database: Database, no_telnyx: bool
) -> None:
    stream = json_log_stream()
    telnyx = FakeTelnyx(fail=TelnyxTransportError("streaming_stop", OSError("down")))
    async with rig(database, status="transferred", telnyx=telnyx, no_telnyx=no_telnyx) as r:
        await r.finish(ended("transferred", "system"))
        assert r.signalled() == [("transferred", "system")]
        assert r.telnyx.stream_stops == []
    events = {e["event"] for e in log_events(stream)}
    expected = "media.streaming_stop_unavailable" if no_telnyx else "media.streaming_stop_failed"
    assert expected in events


async def test_an_unresolvable_account_skips_the_streaming_stop(database: Database) -> None:
    stream = json_log_stream()
    telnyx = FakeTelnyx(account_failure=UnknownTelnyxAccount(uuid.UUID(int=9)))
    async with rig(database, status="transferred", telnyx=telnyx) as r:
        await r.finish(ended("transferred", "system"))
        assert r.signalled() == [("transferred", "system")]
    [failed] = [e for e in log_events(stream) if e["event"] == "media.streaming_stop_failed"]
    assert failed["error_type"] == "UnknownTelnyxAccount"


def hangup_event(r: Rig, *, leg: str = "primary", cause: str = "time_limit", event_id: str = "e-1") -> bytes:
    event = CarrierEvent(
        type="call.hangup",
        leg=leg,  # type: ignore[arg-type]
        call_id=r.call.call_id,
        org_id=r.call.org_id,
        telnyx_event_id=event_id,
        occurred_at=r.clock.now(),
        payload={"hangup_cause": cause},
    )
    return event.model_dump_json().encode()


async def test_a_hangup_event_within_3_s_after_the_stream_closed_is_used(database: Database) -> None:
    async with rig(database) as r:
        finishing = asyncio.ensure_future(r.finish(STREAM_CLOSED))
        await r.clock.wait_for_sleep(STREAM_CLOSED_WAIT_S)
        r.session.feed.on_carrier(hangup_event(r, leg="transfer_target", event_id="e-0"))
        r.session.feed.on_carrier(hangup_event(r))
        await finishing
        call = await load_call(database, r.call)
        assert (call.end_reason, call.ended_by) == ("max_duration", "timeout")
        assert call.ended_at == r.clock.now(), "no leg row: the routine's clock"
        assert r.telnyx.hangups == []
        assert r.signalled() == [("max_duration", "timeout")]


async def test_no_hangup_within_3_s_after_the_stream_closed_hangs_up_as_error(database: Database) -> None:
    async with rig(database) as r:
        finishing = asyncio.ensure_future(r.finish(STREAM_CLOSED))
        await r.clock.wait_for_sleep(STREAM_CLOSED_WAIT_S)
        await r.clock.run_for(STREAM_CLOSED_WAIT_S)
        await finishing
        call = await load_call(database, r.call)
        assert (call.end_reason, call.error_message) == ("error", STREAM_LOST_MESSAGE)
        assert r.telnyx.hangups == hangup_command(r)
        assert r.signalled() == [("error", "error")]


@pytest.mark.parametrize("end", [OWNERSHIP_LOST, ended("worker_lost", "error")], ids=["lost", "drain"])
async def test_a_lost_call_is_left_to_the_workflow(database: Database, end: SessionEnd) -> None:
    async with rig(database) as r:
        await r.finish(end)
        call = await load_call(database, r.call)
        assert (call.status, call.end_reason) == ("in_progress", None)
        assert r.telnyx.hangups == []
        assert r.ended_events() == []
        assert not await r.slot_held()
        assert r.signalled() == [("worker_lost", "error")]


async def test_an_already_ended_call_is_not_rewritten(database: Database) -> None:
    async with rig(database, status="completed", end_reason="user_hangup", ended_by="user") as r:
        await r.finish(ended("agent_end_call", "agent"))
        call = await load_call(database, r.call)
        assert call.end_reason == "user_hangup"
        assert r.ended_events() == []
        assert r.telnyx.hangups == hangup_command(r), "Telnyx dedupes; an ended leg answers already-ended"
        assert not await r.slot_held()
        assert r.signalled() == [("agent_end_call", "agent")]


async def test_a_stored_end_reason_wins_and_is_signalled(database: Database) -> None:
    async with rig(database, end_reason="api_stop", ended_by="api") as r:
        await r.finish(ended("agent_end_call", "agent"))
        assert (await load_call(database, r.call)).end_reason == "api_stop"
        assert r.ended_events() == [{"end_reason": "api_stop", "ended_by": "api"}]
        assert r.signalled() == [("api_stop", "api")]


async def test_an_unanswered_call_is_hung_up_but_not_written(database: Database) -> None:
    async with rig(database, status="ringing", answered_at=None) as r:
        await r.finish(ended("agent_end_call", "agent"))
        call = await load_call(database, r.call)
        assert (call.status, call.end_reason) == ("ringing", None)
        assert r.telnyx.hangups == hangup_command(r)
        assert r.ended_events() == []
        assert r.signalled() == [("agent_end_call", "agent")]


async def test_a_failed_body_ends_as_error(database: Database) -> None:
    async with rig(database) as r:
        await r.finish(None)
        call = await load_call(database, r.call)
        assert (call.end_reason, call.ended_by) == ("error", "error")
        assert call.error_message == "The call pipeline failed."
        assert r.telnyx.hangups == hangup_command(r)
        assert r.signalled() == [("error", "error")]


async def test_an_unknown_reason_ends_as_error(database: Database) -> None:
    async with rig(database) as r:
        await r.finish(ended("mystery", "system"))
        assert (await load_call(database, r.call)).end_reason == "error"


@pytest.mark.parametrize(
    "failure",
    [TelnyxTransportError("hangup", OSError("down")), UnknownTelnyxAccount(uuid.UUID(int=7))],
    ids=["transport", "unknown_account"],
)
async def test_a_failed_hangup_is_logged_and_the_rest_runs(database: Database, failure: Exception) -> None:
    stream = json_log_stream()
    async with rig(database, telnyx=FakeTelnyx(fail=failure)) as r:
        await r.finish(ended("agent_end_call", "agent"))
        assert (await load_call(database, r.call)).status == "completed"
        assert not await r.slot_held()
        assert r.signalled() == [("agent_end_call", "agent")]
    failed = [e for e in log_events(stream) if e["event"] == "media.hangup_failed"]
    assert [e["error_type"] for e in failed] == [type(failure).__name__]


async def test_without_a_telnyx_factory_the_hangup_is_skipped(database: Database) -> None:
    stream = json_log_stream()
    async with rig(database, no_telnyx=True) as r:
        await r.finish(ended("agent_end_call", "agent"))
        assert r.signalled() == [("agent_end_call", "agent")]
    assert "media.hangup_unavailable" in {e["event"] for e in log_events(stream)}


async def test_an_already_ended_leg_is_fine(database: Database) -> None:
    stream = json_log_stream()
    async with rig(database, telnyx=FakeTelnyx(already_ended=True)) as r:
        await r.finish(ended("agent_end_call", "agent"))
    sent = [e for e in log_events(stream) if e["event"] == "media.hangup_sent"]
    assert [e["result"] for e in sent] == ["already_ended"]


async def test_a_database_failure_is_logged_and_the_rest_runs(database: Database) -> None:
    stream = json_log_stream()
    async with rig(database, broken_db=True) as r:
        await r.finish(ended("agent_end_call", "agent"))
        assert r.telnyx.hangups == hangup_command(r)
        assert r.ended_events() == []
        assert not await r.slot_held()
        assert r.signalled() == [("agent_end_call", "agent")]
    failed = [e for e in log_events(stream) if e["event"] == "media.end_record_failed"]
    assert [e["error_type"] for e in failed] == ["OperationalError"]


async def test_event_and_flush_failures_are_logged(database: Database) -> None:
    stream = json_log_stream()
    async with rig(database, events=FakeEvents(fail=True)) as r:
        await r.finish(ended("agent_end_call", "agent"))
        assert (await load_call(database, r.call)).status == "completed"
        assert r.signalled() == [("agent_end_call", "agent")]
    events = {e["event"] for e in log_events(stream)}
    assert {"media.event_publish_failed", "media.call_flush_failed"} <= events


async def test_redis_failures_are_logged_and_the_rest_runs(database: Database) -> None:
    stream = json_log_stream()
    async with rig(database) as r:
        server = fakeredis.FakeServer()
        broken = fakeredis.FakeAsyncRedis(server=server)
        server.connected = False
        r.ender = CallEnd(
            database=database,
            redis=broken,
            signaller=r.signaller,
            clock=r.clock,
            telnyx=cast("TelnyxClientFactory", r.telnyx),
        )
        await r.finish(ended("agent_end_call", "agent"))
        assert (await load_call(database, r.call)).status == "completed"
        assert r.signalled() == [("agent_end_call", "agent")]
    events = {e["event"] for e in log_events(stream)}
    assert {"media.carrier_state_unavailable", "media.slot_release_failed"} <= events
