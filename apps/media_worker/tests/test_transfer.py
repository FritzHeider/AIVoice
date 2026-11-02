"""Cold transfer on the media worker (specs/06 §10.1 Decision P05-T13): caller id, time limit, ColdTransfer
outcomes, and MediaActions routing."""

from __future__ import annotations

import asyncio
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from clock_helpers import ManualClock
from media_helpers import call_context
from obs_helpers import memory_provider
from service_helpers import json_log_stream, log_events

from va_common.time import utcnow
from va_domain.call_context import CallContext
from va_domain.carrier import CarrierEvent, CarrierLeg
from va_engine.models.results import TransferRequest as TransferAction
from va_events import DomainEvent, EventPublishError
from va_media.actions import MediaActions
from va_media.carrier_state import CarrierSnapshot
from va_media.carrier_watch import CarrierWatch
from va_media.channels import CallFeed
from va_media.transfer import (
    MAX_TIME_LIMIT_S,
    MIN_TIME_LIMIT_S,
    TRANSFER_OUTCOME_GRACE_S,
    TRANSFER_TIMEOUT_S,
    ColdTransfer,
    StatusStart,
    TransferOutcome,
    time_limit_s,
    transfer_caller_id,
)
from va_telnyx.client_state import command_id, decode
from va_telnyx.errors import TelnyxApiError
from va_telnyx.models import CommandResult, TransferRequest

CCID = "v3:primary-ccid"
TARGET = "+14155550177"
NOW = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)


@dataclass
class RecordingTelnyx:
    requests: list[tuple[str, TransferRequest]] = field(default_factory=list)
    fail: bool = False

    async def transfer(self, call_control_id: str, request: TransferRequest) -> CommandResult:
        if self.fail:
            raise TelnyxApiError(422, (), "transfer")
        self.requests.append((call_control_id, request))
        return CommandResult(result="ok")


@dataclass
class RecordingStatus:
    start_result: StatusStart = "started"
    log: list[str] = field(default_factory=list)
    calls: list[uuid.UUID] = field(default_factory=list)

    async def start(self, call_id: uuid.UUID) -> StatusStart:
        self.log.append("start")
        self.calls.append(call_id)
        return self.start_result

    async def revert(self, call_id: uuid.UUID) -> None:
        self.log.append("revert")
        self.calls.append(call_id)


@dataclass
class RecordingEvents:
    events: list[DomainEvent] = field(default_factory=list)
    fail: bool = False

    async def publish(self, event: DomainEvent) -> DomainEvent:
        if self.fail:
            raise EventPublishError(event, "stream")
        self.events.append(event)
        return event


@dataclass
class Rig:
    transfer: ColdTransfer
    telnyx: RecordingTelnyx
    status: RecordingStatus
    events: RecordingEvents
    queue: asyncio.Queue[CarrierEvent]
    clock: ManualClock
    context: CallContext


def rig(
    *,
    direction: Literal["outbound", "inbound", "web"] = "outbound",
    mode: Literal["agent_number", "original_caller"] = "agent_number",
    telnyx: bool = True,
    deadline: timedelta = timedelta(minutes=10),
) -> Rig:
    clock = ManualClock()
    context = call_context(uuid.uuid4(), direction=direction)
    sender, status, events = RecordingTelnyx(), RecordingStatus(), RecordingEvents()
    queue: asyncio.Queue[CarrierEvent] = asyncio.Queue()
    transfer = ColdTransfer(
        context=context,
        call_control_id=CCID,
        telnyx=sender if telnyx else None,
        transfer_events=queue,
        status=status,
        events=events,
        caller_id_mode=mode,
        deadline=clock.now() + deadline,
        clock=clock,
    )
    return Rig(transfer, sender, status, events, queue, clock, context)


def target_event(context: CallContext, event_type: str, leg: CarrierLeg = "transfer_target") -> CarrierEvent:
    return CarrierEvent(
        type=event_type,
        leg=leg,
        call_id=context.call_id,
        org_id=context.org_id,
        telnyx_event_id=secrets.token_hex(8),
        occurred_at=utcnow(),
    )


async def sent(r: Rig, count: int = 1) -> None:
    """Wait (bounded, event-loop turns only) until ``count`` transfer commands went out."""
    for _ in range(200):
        if len(r.telnyx.requests) >= count:
            return
        await asyncio.sleep(0)
    message = "the transfer command was not sent"
    raise AssertionError(message)


@pytest.mark.parametrize(
    ("direction", "mode", "expected"),
    [
        ("outbound", "agent_number", "+14155550199"),
        ("outbound", "original_caller", "+14155550100"),
        ("inbound", "agent_number", "+14155550100"),
        ("inbound", "original_caller", "+14155550199"),
        ("web", "agent_number", "+14155550100"),
        ("web", "original_caller", "+14155550199"),
    ],
)
def test_the_caller_id_follows_the_mode_and_direction(
    direction: Literal["outbound", "inbound", "web"],
    mode: Literal["agent_number", "original_caller"],
    expected: str,
) -> None:
    # call_context: to_number +14155550100, from_number +14155550199.
    assert transfer_caller_id(call_context(uuid.uuid4(), direction=direction), mode) == expected


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (timedelta(minutes=10), 600),
        (timedelta(seconds=90, milliseconds=200), 91),
        (timedelta(seconds=5), MIN_TIME_LIMIT_S),
        (timedelta(seconds=-20), MIN_TIME_LIMIT_S),
        (timedelta(hours=9), MAX_TIME_LIMIT_S),
    ],
)
def test_the_time_limit_is_the_rest_of_the_call_within_bounds(remaining: timedelta, expected: int) -> None:
    assert time_limit_s(NOW + remaining, NOW) == expected


async def test_a_bridged_transfer_marks_the_status_emits_the_event_and_sends_the_command() -> None:
    r = rig()
    provider, exporter = memory_provider()
    r.transfer = ColdTransfer(
        context=r.context,
        call_control_id=CCID,
        telnyx=r.telnyx,
        transfer_events=r.queue,
        status=r.status,
        events=r.events,
        caller_id_mode="agent_number",
        deadline=r.clock.now() + timedelta(minutes=10),
        clock=r.clock,
        tracer_provider=provider,
    )
    stream = json_log_stream()
    task = asyncio.ensure_future(r.transfer.run(TARGET))
    await sent(r)
    r.queue.put_nowait(target_event(r.context, "call.initiated"))
    r.queue.put_nowait(target_event(r.context, "call.bridged"))
    assert await asyncio.wait_for(task, 5) == "bridged"

    assert r.status.log == ["start"]
    [event] = r.events.events
    assert (event.type.value, event.call_id, event.data) == (
        "call.transfer_started",
        r.context.call_id,
        {"type": "cold", "target": TARGET},
    )
    [(ccid, request)] = r.telnyx.requests
    assert ccid == CCID
    assert (request.to, request.from_, request.timeout_secs, request.time_limit_secs) == (
        TARGET,
        "+14155550199",
        TRANSFER_TIMEOUT_S,
        600,
    )
    assert request.target_leg_client_state is not None
    target_state = decode(request.target_leg_client_state)
    assert (target_state.call_id, target_state.leg, target_state.org_id) == (
        r.context.call_id,
        "transfer_target",
        r.context.org_id,
    )
    assert request.client_state is not None
    assert decode(request.client_state).leg == "primary"
    assert request.command_id == command_id("transfer", r.context.call_id, "primary", 0)
    assert r.transfer.transfers == 1
    [span] = exporter.get_finished_spans()
    assert span.name == "call.transfer"
    assert dict(span.attributes or {}) == {"va.transfer.type": "cold", "va.transfer.outcome": "bridged"}
    assert TARGET not in stream.getvalue()
    assert "+14155550199" not in stream.getvalue()
    [finished] = [e for e in log_events(stream) if e["event"] == "media.transfer_finished"]
    assert (finished["outcome"], finished["attempt"]) == ("bridged", 1)


async def test_a_target_hangup_reverts_and_the_next_transfer_gets_a_new_command_id() -> None:
    r = rig()
    task = asyncio.ensure_future(r.transfer.run(TARGET))
    await sent(r)
    r.queue.put_nowait(target_event(r.context, "call.hangup"))
    assert await asyncio.wait_for(task, 5) == "target_hangup"
    assert r.status.log == ["start", "revert"]
    assert r.status.calls == [r.context.call_id, r.context.call_id]

    # A late target event of the first attempt is dropped before the second one is sent.
    r.queue.put_nowait(target_event(r.context, "call.bridged"))
    task = asyncio.ensure_future(r.transfer.run(TARGET))
    await sent(r, 2)
    assert r.queue.empty()
    r.queue.put_nowait(target_event(r.context, "call.bridged"))
    assert await asyncio.wait_for(task, 5) == "bridged"
    assert [request.command_id for _, request in r.telnyx.requests] == [
        command_id("transfer", r.context.call_id, "primary", 0),
        command_id("transfer", r.context.call_id, "primary", 1),
    ]


async def test_no_outcome_within_the_ring_timeout_and_grace_is_a_timeout() -> None:
    r = rig()
    task = asyncio.ensure_future(r.transfer.run(TARGET))
    wait_s = TRANSFER_TIMEOUT_S + TRANSFER_OUTCOME_GRACE_S
    await r.clock.wait_for_sleep(wait_s)
    await r.clock.run_for(wait_s - 0.1)
    assert not task.done()
    await r.clock.run_for(0.1)
    assert await asyncio.wait_for(task, 5) == "timeout"
    assert r.status.log == ["start", "revert"]


async def test_a_refused_command_reverts_and_fails() -> None:
    r = rig()
    r.telnyx.fail = True
    stream = json_log_stream()
    assert await r.transfer.run(TARGET) == "command_failed"
    assert r.status.log == ["start", "revert"]
    [failure] = [e for e in log_events(stream) if e["event"] == "media.transfer_command_failed"]
    assert failure["error_type"] == "TelnyxApiError"


@pytest.mark.parametrize(
    ("start", "outcome"), [("not_active", "not_active"), ("unavailable", "command_failed")]
)
async def test_a_call_that_cannot_enter_transferring_sends_nothing(
    start: StatusStart, outcome: TransferOutcome
) -> None:
    r = rig()
    r.status.start_result = start
    assert await r.transfer.run(TARGET) == outcome
    assert r.status.log == ["start"]
    assert r.telnyx.requests == []
    assert r.events.events == []


async def test_without_telnyx_nothing_changes() -> None:
    r = rig(telnyx=False)
    assert await r.transfer.run(TARGET) == "command_failed"
    assert r.status.log == []
    assert r.events.events == []


async def test_an_event_publish_failure_never_stops_the_transfer() -> None:
    r = rig(mode="original_caller")
    r.events.fail = True
    task = asyncio.ensure_future(r.transfer.run(TARGET))
    await sent(r)
    r.queue.put_nowait(target_event(r.context, "call.bridged"))
    assert await asyncio.wait_for(task, 5) == "bridged"
    assert r.telnyx.requests[0][1].from_ == "+14155550100"


async def test_without_status_and_events_the_transfer_still_runs() -> None:
    clock = ManualClock()
    context = call_context(uuid.uuid4())
    queue: asyncio.Queue[CarrierEvent] = asyncio.Queue()
    sender = RecordingTelnyx()
    transfer = ColdTransfer(
        context=context,
        call_control_id=CCID,
        telnyx=sender,
        transfer_events=queue,
        status=None,
        events=None,
        caller_id_mode="agent_number",
        deadline=clock.now(),
        clock=clock,
    )
    queue.put_nowait(target_event(context, "call.answered"))
    task = asyncio.ensure_future(transfer.run(TARGET))
    for _ in range(200):
        if sender.requests:
            break
        await asyncio.sleep(0)
    queue.put_nowait(target_event(context, "call.hangup"))
    assert await asyncio.wait_for(task, 5) == "target_hangup"
    assert sender.requests[0][1].time_limit_secs == MIN_TIME_LIMIT_S


async def test_the_carrier_watch_queues_target_leg_events() -> None:
    call_id = uuid.uuid4()
    feed = CallFeed(call_id)
    watch = CarrierWatch(feed, CarrierSnapshot())
    context = call_context(call_id)
    task = asyncio.ensure_future(watch.run())
    try:
        feed.on_carrier(target_event(context, "call.answered").model_dump_json())
        feed.on_carrier(target_event(context, "call.bridged").model_dump_json())
        first = await asyncio.wait_for(watch.transfer_events.get(), 5)
        second = await asyncio.wait_for(watch.transfer_events.get(), 5)
    finally:
        task.cancel()
        await asyncio.wait({task})
    assert [first.type, second.type] == ["call.answered", "call.bridged"]
    assert not watch.answered.is_set()


# MediaActions routing


@dataclass
class ScriptedTransfer:
    outcome: TransferOutcome
    targets: list[str] = field(default_factory=list)

    async def run(self, target: str) -> TransferOutcome:
        self.targets.append(target)
        return self.outcome


def actions_with(transfer: ScriptedTransfer | None) -> tuple[MediaActions, list[str]]:
    calls: list[str] = []
    actions = MediaActions(
        call_id=uuid.uuid4(),
        call_control_id=CCID,
        telnyx=None,
        clock=ManualClock(),
        transfer=transfer,  # type: ignore[arg-type]
    )
    actions.bind(
        on_transferred=lambda: calls.append("transferred"),
        on_transfer_failed=lambda: calls.append("failed"),
    )
    return actions, calls


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("bridged", ["transferred"]),
        ("target_hangup", ["failed"]),
        ("timeout", ["failed"]),
        ("command_failed", ["failed"]),
        ("not_active", []),
    ],
)
async def test_actions_route_the_transfer_outcome(outcome: TransferOutcome, expected: list[str]) -> None:
    transfer = ScriptedTransfer(outcome)
    actions, calls = actions_with(transfer)
    await actions.handle(TransferAction(target=TARGET, label="sales"))
    assert transfer.targets == [TARGET]
    assert calls == expected


async def test_a_transfer_without_a_cold_transfer_fails_and_unbound_callbacks_are_skipped() -> None:
    stream = json_log_stream()
    actions, calls = actions_with(None)
    await actions.handle(TransferAction(target=TARGET))
    assert calls == ["failed"]
    assert "media.transfer_unavailable" in [e["event"] for e in log_events(stream)]
    unbound = MediaActions(call_id=uuid.uuid4(), call_control_id=CCID, telnyx=None, clock=ManualClock())
    await unbound.handle(TransferAction(target=TARGET))
