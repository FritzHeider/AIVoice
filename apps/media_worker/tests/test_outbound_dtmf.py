"""Outbound DTMF, the carrier watch, the greeting gate, and the media action handler (specs/07 §11,
specs/06 §9; Decision P05-T10b). Everything runs on a :class:`ManualClock`."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from clock_helpers import ManualClock
from media_helpers import call_context
from service_helpers import json_log_stream, log_events

from va_common.time import utcnow
from va_domain.call_context import CallContext
from va_domain.carrier import CarrierEvent, CarrierLeg
from va_engine.models.results import PressButton, TransferRequest
from va_media.actions import MediaActions
from va_media.carrier_state import CarrierSnapshot
from va_media.carrier_watch import CarrierWatch
from va_media.channels import CallFeed
from va_media.dtmf_out import DTMF_LONG_PAUSE_S, DTMF_PAUSE_S, dtmf_steps, send_dtmf_steps
from va_media.greeting import ANSWER_WAIT_S, PRECALL_SETTLE_S, GreetingGate
from va_telnyx.client_state import command_id
from va_telnyx.errors import TelnyxApiError, TelnyxTransportError

CCID = "v3:call-control-id"


@dataclass
class Sent:
    call_control_id: str
    digits: str
    command_id: str
    at: float


@dataclass
class RecordingSender:
    """``send_dtmf`` recording each command with the clock time; ``fail_at`` raises on that group."""

    clock: ManualClock
    sent: list[Sent] = field(default_factory=list)
    fail_at: int | None = None
    error: Exception = field(default_factory=lambda: TelnyxApiError(422, (), "send_dtmf"))

    async def send_dtmf(
        self, call_control_id: str, digits: str, duration_millis: int = 250, *, command_id: str
    ) -> object:
        assert duration_millis == 250
        if self.fail_at is not None and len(self.sent) == self.fail_at:
            raise self.error
        self.sent.append(Sent(call_control_id, digits, command_id, self.clock.monotonic()))
        return None


@pytest.mark.parametrize(
    ("sequence", "steps"),
    [
        ("12w3ww4", ("12", DTMF_PAUSE_S, "3", DTMF_PAUSE_S, DTMF_PAUSE_S, "4")),
        ("wW1", (DTMF_PAUSE_S, DTMF_LONG_PAUSE_S, "1")),
        ("*9#AD", ("*9#AD",)),
    ],
)
def test_steps_group_digits_between_pauses(sequence: str, steps: tuple[object, ...]) -> None:
    assert dtmf_steps(sequence) == steps


@pytest.mark.parametrize("sequence", ["", "12x", "1 2", "e"])
def test_invalid_sequences_are_refused(sequence: str) -> None:
    with pytest.raises(ValueError, match="DTMF sequence"):
        dtmf_steps(sequence)


async def test_groups_go_out_with_the_pauses_between_them() -> None:
    clock = ManualClock()
    sender = RecordingSender(clock)
    task = asyncio.ensure_future(
        send_dtmf_steps(sender, CCID, dtmf_steps("12w3W4"), clock=clock, command_id_for=lambda i: f"c{i}")
    )
    await clock.wait_for_sleep(DTMF_PAUSE_S)
    assert [s.digits for s in sender.sent] == ["12"]
    await clock.run_for(DTMF_PAUSE_S)
    await clock.wait_for_sleep(DTMF_LONG_PAUSE_S)
    await clock.run_for(DTMF_LONG_PAUSE_S)
    assert await task == 3
    assert [(s.digits, s.command_id, s.at) for s in sender.sent] == [
        ("12", "c0", 0.0),
        ("3", "c1", DTMF_PAUSE_S),
        ("4", "c2", DTMF_PAUSE_S + DTMF_LONG_PAUSE_S),
    ]
    assert {s.call_control_id for s in sender.sent} == {CCID}


def carrier_event(call_id: uuid.UUID, event_type: str, leg: CarrierLeg = "primary") -> str:
    return CarrierEvent(
        type=event_type,
        leg=leg,
        call_id=call_id,
        org_id=uuid.uuid4(),
        telnyx_event_id=secrets.token_hex(8),
        occurred_at=utcnow(),
    ).model_dump_json()


@contextlib.asynccontextmanager
async def watching(watch: CarrierWatch) -> AsyncIterator[asyncio.Task[None]]:
    task = asyncio.ensure_future(watch.run())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_an_answered_snapshot_is_answered_at_once() -> None:
    call_id = uuid.uuid4()
    watch = CarrierWatch(CallFeed(call_id), CarrierSnapshot({"answered": "1"}))
    assert watch.answered.is_set()


async def test_the_primary_answer_event_sets_answered() -> None:
    call_id = uuid.uuid4()
    feed = CallFeed(call_id)
    watch = CarrierWatch(feed, CarrierSnapshot())
    async with watching(watch):
        feed.on_carrier(carrier_event(call_id, "call.answered", leg="transfer_target"))
        feed.on_carrier(carrier_event(call_id, "call.machine.detection.ended"))
        await settle()
        assert not watch.answered.is_set()
        assert watch.events_seen == 2
        feed.on_carrier(carrier_event(call_id, "call.answered"))
        await settle()
        assert watch.answered.is_set()
        feed.on_carrier(carrier_event(call_id, "call.answered"))
        await settle()
        assert watch.events_seen == 4


@dataclass
class GateRig:
    clock: ManualClock
    feed: CallFeed
    watch: CarrierWatch
    sender: RecordingSender
    context: CallContext

    def gate(self, *, telnyx: bool = True) -> GreetingGate:
        return GreetingGate(
            context=self.context,
            call_control_id=CCID,
            watch=self.watch,
            telnyx=self.sender if telnyx else None,
            clock=self.clock,
        )


def gate_rig(*, answered: bool = False, direction: str = "outbound", **config: Any) -> GateRig:
    clock = ManualClock()
    context = call_context(uuid.uuid4(), **config).model_copy(update={"direction": direction})
    feed = CallFeed(context.call_id)
    watch = CarrierWatch(feed, CarrierSnapshot({"answered": "1"} if answered else {}))
    return GateRig(clock, feed, watch, RecordingSender(clock), context)


async def test_an_answered_call_passes_the_gate_at_once() -> None:
    r = gate_rig(answered=True)
    await r.gate()()
    assert r.clock.monotonic() == 0.0
    assert r.sender.sent == []


async def test_the_gate_waits_for_the_answer_event() -> None:
    r = gate_rig()
    async with watching(r.watch):
        task = asyncio.ensure_future(r.gate()())
        await r.clock.wait_for_sleep(ANSWER_WAIT_S)
        await r.clock.run_for(0.3)
        assert not task.done()
        r.feed.on_carrier(carrier_event(r.context.call_id, "call.answered"))
        await asyncio.wait_for(task, 5)
    assert r.clock.monotonic() == pytest.approx(0.3)


async def test_an_unconfirmed_answer_proceeds_after_the_wait() -> None:
    stream = json_log_stream()
    r = gate_rig()
    task = asyncio.ensure_future(r.gate()())
    await r.clock.wait_for_sleep(ANSWER_WAIT_S)
    await r.clock.run_for(ANSWER_WAIT_S)
    await asyncio.wait_for(task, 5)
    assert r.clock.monotonic() == ANSWER_WAIT_S
    assert "media.answer_unconfirmed" in [e["event"] for e in log_events(stream)]


@pytest.mark.parametrize("direction", ["inbound", "web"])
async def test_inbound_and_web_calls_skip_the_gate(direction: str) -> None:
    r = gate_rig(direction=direction, precall_dtmf_sequence="12")
    await r.gate()()
    assert (r.clock.monotonic(), r.sender.sent) == (0.0, [])


async def test_the_precall_sequence_goes_out_then_settles() -> None:
    stream = json_log_stream()
    r = gate_rig(answered=True, precall_dtmf_sequence="12w3")
    task = asyncio.ensure_future(r.gate()())
    await r.clock.wait_for_sleep(DTMF_PAUSE_S)
    await r.clock.run_for(DTMF_PAUSE_S)
    await r.clock.wait_for_sleep(PRECALL_SETTLE_S)
    assert not task.done()
    await r.clock.run_for(PRECALL_SETTLE_S)
    await asyncio.wait_for(task, 5)
    call_id = r.context.call_id
    assert [(s.digits, s.command_id, s.at) for s in r.sender.sent] == [
        ("12", command_id("precall_dtmf", call_id, "primary", 0), 0.0),
        ("3", command_id("precall_dtmf", call_id, "primary", 1), DTMF_PAUSE_S),
    ]
    assert r.clock.monotonic() == DTMF_PAUSE_S + PRECALL_SETTLE_S
    events = log_events(stream)
    assert [e["groups"] for e in events if e["event"] == "media.precall_dtmf_sent"] == [2]
    # Compare whole values: the timestamp of a line may contain the digits (e.g. 08:12:36).
    assert '"12"' not in stream.getvalue()
    assert all("digits" not in e for e in events)


async def test_a_telnyx_failure_ends_the_precall_sequence_and_the_gate_goes_on() -> None:
    stream = json_log_stream()
    r = gate_rig(answered=True, precall_dtmf_sequence="1w2")
    r.sender.fail_at = 1
    r.sender.error = TelnyxTransportError("send_dtmf", ConnectionError("down"))
    task = asyncio.ensure_future(r.gate()())
    await r.clock.wait_for_sleep(DTMF_PAUSE_S)
    await r.clock.run_for(DTMF_PAUSE_S)
    await asyncio.wait_for(task, 5)
    assert [s.digits for s in r.sender.sent] == ["1"]
    assert r.clock.monotonic() == DTMF_PAUSE_S
    [failed] = [e for e in log_events(stream) if e["event"] == "media.precall_dtmf_failed"]
    assert failed["error_type"] == "TelnyxTransportError"


async def test_without_a_telnyx_client_the_precall_sequence_is_skipped() -> None:
    stream = json_log_stream()
    r = gate_rig(answered=True, precall_dtmf_sequence="1")
    await r.gate(telnyx=False)()
    assert r.sender.sent == []
    assert "media.precall_dtmf_skipped" in [e["event"] for e in log_events(stream)]


def actions_rig(*, telnyx: bool = True) -> tuple[MediaActions, RecordingSender, ManualClock, uuid.UUID]:
    clock = ManualClock()
    sender = RecordingSender(clock)
    call_id = uuid.uuid4()
    actions = MediaActions(
        call_id=call_id, call_control_id=CCID, telnyx=sender if telnyx else None, clock=clock
    )
    return actions, sender, clock, call_id


async def test_button_presses_send_their_groups_with_distinct_command_ids() -> None:
    actions, sender, clock, call_id = actions_rig()
    await actions.handle(PressButton(digits="1"))
    task = asyncio.ensure_future(actions.handle(PressButton(digits="2W3")))
    await clock.wait_for_sleep(DTMF_LONG_PAUSE_S)
    await clock.run_for(DTMF_LONG_PAUSE_S)
    await asyncio.wait_for(task, 5)
    assert [(s.digits, s.command_id) for s in sender.sent] == [
        ("1", command_id("press_button", call_id, "primary", 0, 0)),
        ("2", command_id("press_button", call_id, "primary", 1, 0)),
        ("3", command_id("press_button", call_id, "primary", 1, 1)),
    ]
    assert actions.presses == 2


async def test_a_failed_press_is_logged_and_swallowed() -> None:
    stream = json_log_stream()
    actions, sender, _, _ = actions_rig()
    sender.fail_at = 0
    await actions.handle(PressButton(digits="9"))
    await actions.handle(PressButton(digits="x"))
    failures = [e["error_type"] for e in log_events(stream) if e["event"] == "media.press_button_failed"]
    assert failures == ["TelnyxApiError", "ValueError"]
    assert actions.presses == 2
    assert '"9"' not in stream.getvalue()


async def test_presses_without_telnyx_and_other_actions_send_nothing() -> None:
    stream = json_log_stream()
    actions, sender, _, _ = actions_rig(telnyx=False)
    await actions.handle(PressButton(digits="1"))
    await actions.handle(TransferRequest(target="+14155550123"))
    assert sender.sent == []
    events = log_events(stream)
    assert "media.press_button_skipped" in [e["event"] for e in events]
    [unsupported] = [e for e in events if e["event"] == "media.action_unsupported"]
    assert unsupported["kind"] == "transfer_call"
