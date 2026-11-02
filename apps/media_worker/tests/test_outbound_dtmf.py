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
from va_engine.models.results import PressButton
from va_media.actions import MediaActions
from va_media.carrier_state import CarrierSnapshot
from va_media.carrier_watch import CarrierWatch
from va_media.channels import CallFeed
from va_media.dtmf_out import DTMF_LONG_PAUSE_S, DTMF_PAUSE_S, dtmf_steps, send_dtmf_steps
from va_media.greeting import AMD_WAIT_S, ANSWER_WAIT_S, PRECALL_SETTLE_S, GreetingGate
from va_telnyx.client_state import command_id
from va_telnyx.errors import TelnyxApiError, TelnyxTransportError
from va_voice.pipeline import BrainAction

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


def gate_rig(
    *, answered: bool = False, direction: str = "outbound", answered_by: str | None = "human", **config: Any
) -> GateRig:
    """A gate over a fresh context; the snapshot holds ``answered`` (when set) and ``answered_by`` (the edge
    stores it with the premium AMD verdict; ``None``: no verdict yet)."""
    clock = ManualClock()
    context = call_context(uuid.uuid4(), **config).model_copy(update={"direction": direction})
    feed = CallFeed(context.call_id)
    fields = {"answered": "1"} if answered else {}
    if answered_by is not None:
        fields["answered_by"] = answered_by
    watch = CarrierWatch(feed, CarrierSnapshot(fields))
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
    await actions.handle(BrainAction("hold"))
    assert sender.sent == []
    events = log_events(stream)
    assert "media.press_button_skipped" in [e["event"] for e in events]
    [unsupported] = [e for e in events if e["event"] == "media.action_unsupported"]
    assert unsupported["kind"] == "hold"


# AMD (specs/07 §11 step 2, §14; Decision P05-T10c)


def fresh_watch() -> tuple[uuid.UUID, CallFeed, CarrierWatch, list[str]]:
    """A watch over an empty snapshot whose ``on_verdict`` calls are recorded in the returned list."""
    call_id = uuid.uuid4()
    feed = CallFeed(call_id)
    watch = CarrierWatch(feed, CarrierSnapshot())
    seen: list[str] = []
    watch.on_verdict = seen.append
    return call_id, feed, watch, seen


def amd_event(call_id: uuid.UUID, event_type: str, result: str | None, leg: CarrierLeg = "primary") -> str:
    return CarrierEvent(
        type=event_type,
        leg=leg,
        call_id=call_id,
        org_id=uuid.uuid4(),
        telnyx_event_id=secrets.token_hex(8),
        occurred_at=utcnow(),
        payload={} if result is None else {"result": result},
    ).model_dump_json()


@pytest.mark.parametrize(
    ("fields", "verdict", "beep"),
    [
        ({}, None, False),
        ({"answered_by": "human", "amd_result": "human_residence"}, "human", False),
        ({"answered_by": "voicemail", "amd_result": "machine"}, "machine", False),
        ({"answered_by": "fax"}, "fax", False),
        ({"answered_by": "unknown", "amd_result": "silence"}, "unknown", False),
        ({"amd_result": "human_residence"}, "human", False),
        ({"amd_result": "machine", "greeting_ended": "1"}, "machine", True),
        ({"amd_result": "unknown"}, "unknown", False),
        ({"beep_detected": "1"}, None, True),
    ],
)
def test_the_snapshot_holds_the_verdict_and_the_beep(
    fields: dict[str, str], verdict: str | None, beep: bool
) -> None:
    watch = CarrierWatch(CallFeed(uuid.uuid4()), CarrierSnapshot(fields))
    assert watch.verdict == verdict
    assert watch.amd.is_set() is (verdict is not None)
    assert watch.beep.is_set() is beep


@pytest.mark.parametrize(
    ("event_type", "result", "verdict"),
    [
        ("call.machine.premium.detection.ended", "machine", "machine"),
        ("call.machine.premium.detection.ended", "human_business", "human"),
        ("call.machine.premium.detection.ended", "fax_detected", "fax"),
        ("call.machine.detection.ended", "machine", "machine"),
        ("call.machine.detection.ended", "not_sure", "unknown"),
        ("call.machine.detection.ended", None, "unknown"),
    ],
)
async def test_the_first_primary_detection_sets_the_verdict_once(
    event_type: str, result: str | None, verdict: str
) -> None:
    call_id, feed, watch, seen = fresh_watch()
    async with watching(watch):
        feed.on_carrier(amd_event(call_id, event_type, "machine", leg="transfer_target"))
        await settle()
        assert not watch.amd.is_set()
        feed.on_carrier(amd_event(call_id, event_type, result))
        feed.on_carrier(amd_event(call_id, "call.machine.premium.detection.ended", "human_residence"))
        await settle()
    assert (watch.verdict, seen) == (verdict, [verdict])
    assert watch.amd.is_set()
    assert not watch.beep.is_set()


@pytest.mark.parametrize("event_type", ["call.machine.premium.greeting.ended", "call.machine.greeting.ended"])
async def test_greeting_ended_events_set_the_beep(event_type: str) -> None:
    call_id, feed, watch, seen = fresh_watch()
    async with watching(watch):
        feed.on_carrier(amd_event(call_id, event_type, "beep_detected", leg="warm_proxy"))
        await settle()
        assert not watch.beep.is_set()
        feed.on_carrier(amd_event(call_id, event_type, "beep_detected"))
        feed.on_carrier(amd_event(call_id, event_type, "beep_detected"))
        await settle()
    assert watch.beep.is_set()
    assert (watch.verdict, seen) == (None, [])


async def test_call_screening_is_logged_and_changes_nothing() -> None:
    stream = json_log_stream()
    call_id, feed, watch, seen = fresh_watch()
    async with watching(watch):
        feed.on_carrier(amd_event(call_id, "call.machine.premium.call_screening.detected", None))
        await settle()
    assert (watch.verdict, seen, watch.amd.is_set(), watch.beep.is_set()) == (None, [], False, False)
    assert "media.call_screening" in [e["event"] for e in log_events(stream)]


async def test_the_gate_waits_for_the_amd_verdict() -> None:
    r = gate_rig(answered=True, answered_by=None)
    async with watching(r.watch):
        task = asyncio.ensure_future(r.gate()())
        await r.clock.wait_for_sleep(AMD_WAIT_S)
        await r.clock.run_for(1.2)
        assert not task.done()
        r.feed.on_carrier(
            amd_event(r.context.call_id, "call.machine.premium.detection.ended", "human_residence")
        )
        await asyncio.wait_for(task, 5)
    assert r.clock.monotonic() == pytest.approx(1.2)


async def test_a_pending_verdict_is_treated_as_human_after_three_seconds() -> None:
    stream = json_log_stream()
    r = gate_rig(answered=True, answered_by=None, precall_dtmf_sequence="1")
    task = asyncio.ensure_future(r.gate()())
    await r.clock.wait_for_sleep(AMD_WAIT_S)
    await r.clock.run_for(AMD_WAIT_S)
    await r.clock.wait_for_sleep(PRECALL_SETTLE_S)
    await r.clock.run_for(PRECALL_SETTLE_S)
    await asyncio.wait_for(task, 5)
    assert [s.digits for s in r.sender.sent] == ["1"]
    assert "media.amd_pending" in [e["event"] for e in log_events(stream)]


@pytest.mark.parametrize(
    "config",
    [
        {"ivr_mode": True},
        {"answered_by_enabled": False},
        {"voicemail": {"action": "ignore", "sensitive": False}},
    ],
)
async def test_without_amd_the_gate_does_not_wait(config: dict[str, Any]) -> None:
    r = gate_rig(answered=True, answered_by=None, **config)
    await r.gate()()
    assert r.clock.monotonic() == 0.0


async def test_inbound_calls_never_wait_for_amd() -> None:
    r = gate_rig(direction="inbound", answered_by=None)
    await r.gate()()
    assert r.clock.monotonic() == 0.0


@pytest.mark.parametrize(
    ("answered_by", "action", "sent"),
    [
        ("voicemail", "hangup", False),
        ("voicemail", "leave_message", False),
        ("voicemail", "ignore", True),
        ("fax", "ignore", False),
        ("fax", "hangup", False),
        ("human", "hangup", True),
    ],
)
async def test_precall_dtmf_is_skipped_for_machines_the_voicemail_flow_handles(
    answered_by: str, action: str, sent: bool
) -> None:
    voicemail = {"action": action, "sensitive": True, "message": "Hi"}
    r = gate_rig(answered=True, answered_by=answered_by, precall_dtmf_sequence="1", voicemail=voicemail)
    task = asyncio.ensure_future(r.gate()())
    if sent:
        await r.clock.wait_for_sleep(PRECALL_SETTLE_S)
        await r.clock.run_for(PRECALL_SETTLE_S)
    await asyncio.wait_for(task, 5)
    assert bool(r.sender.sent) is sent
