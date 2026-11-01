"""Pure carrier event mapping (specs/06-telephony-telnyx.md §5, §5.1; §5 Decision P05-T02)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, NamedTuple

import pytest
from telnyx_spec import spec_voice_event_types

from va_db.call_states import END_REASONS, allowed_from
from va_db.models import CallStatus
from va_edge.mapping import (
    AMD_DETECTION_TYPES,
    SIGNALLED_TYPES,
    STORED_ONLY_TYPES,
    VOICE_EVENT_TYPES,
    CallSnapshot,
    CarrierEffects,
    hangup_outcome,
    normalize_amd_result,
    plan_effects,
    premium_answered_by,
)
from va_events.catalog import EventType
from va_telnyx.voice_events import VOICE_PAYLOAD_MODELS, CallHangup, VoicePayload

AT: Final = datetime(2026, 11, 1, 12, 0, 30, tzinfo=UTC)
ANSWERED: Final = AT - timedelta(seconds=20)
CCID: Final = "v3:abc"
SNAPSHOT: Final = CallSnapshot(
    call_id=uuid.UUID(int=1), org_id=uuid.UUID(int=2), status="ringing", leg_role="primary"
)
LIVE: Final = replace(SNAPSHOT, status="in_progress", answered_at=ANSWERED)


def payload(event_type: str, **fields: Any) -> VoicePayload:
    base: dict[str, Any] = (
        {"conference_id": "conf-1", "call_control_id": CCID}
        if event_type.startswith("conference.")
        else {"call_control_id": CCID, "to": "+14155550123"}
    )
    if event_type == "call.dtmf.received":
        base["digit"] = "5"
    return VOICE_PAYLOAD_MODELS[event_type].model_validate({**base, **fields})


def plan(event_type: str, snapshot: CallSnapshot = SNAPSHOT, **fields: Any) -> CarrierEffects:
    return plan_effects(event_type, payload(event_type, **fields), snapshot, occurred_at=AT)


def test_every_spec_event_type_is_mapped() -> None:
    assert spec_voice_event_types() == VOICE_EVENT_TYPES
    assert set(VOICE_PAYLOAD_MODELS) == VOICE_EVENT_TYPES
    assert AMD_DETECTION_TYPES <= SIGNALLED_TYPES <= VOICE_EVENT_TYPES


class Expected(NamedTuple):
    leg: str | None
    call: str | None
    events: tuple[EventType, ...]
    state: dict[str, str]
    publish: bool = True


AMD: Final = EventType.CALL_AMD
CASES: Final[dict[str, tuple[CallSnapshot, dict[str, Any], Expected]]] = {
    "call.initiated": (
        replace(SNAPSHOT, status="dialing"),
        {"direction": "outgoing"},
        Expected("initiated", "ringing", (EventType.CALL_RINGING,), {}),
    ),
    "call.answered": (
        SNAPSHOT,
        {},
        Expected(
            "answered",
            "in_progress",
            (EventType.CALL_ANSWERED,),
            {"answered": "1", "answered_at": AT.isoformat()},
        ),
    ),
    "call.bridged": (SNAPSHOT, {}, Expected("bridged", None, (), {"primary_bridged": "1"})),
    "call.hangup": (
        LIVE,
        {"hangup_cause": "normal_clearing", "hangup_source": "callee"},
        Expected(
            "hangup",
            "completed",
            (EventType.CALL_ENDED,),
            {"primary_hangup": "1", "hangup_cause": "normal_clearing"},
        ),
    ),
    "call.machine.detection.ended": (
        LIVE,
        {"result": "human"},
        Expected(None, None, (AMD,), {"amd_result": "human_residence"}),
    ),
    "call.machine.greeting.ended": (LIVE, {}, Expected(None, None, (AMD,), {"greeting_ended": "1"})),
    "call.machine.premium.detection.ended": (
        LIVE,
        {"result": "machine"},
        Expected(None, "fields", (AMD,), {"amd_result": "machine", "answered_by": "voicemail"}),
    ),
    "call.machine.premium.greeting.ended": (LIVE, {}, Expected(None, None, (AMD,), {"beep_detected": "1"})),
    "call.machine.premium.call_screening.detected": (
        LIVE,
        {},
        Expected(None, None, (AMD,), {"screening": "1"}),
    ),
    "call.dtmf.received": (LIVE, {}, Expected(None, None, (), {})),
    "call.recording.saved": (LIVE, {}, Expected(None, None, (), {})),
    "call.recording.error": (
        LIVE,
        {"reason": "upload_failed"},
        Expected(None, None, (EventType.CALL_ERROR,), {}),
    ),
    "call.playback.started": (LIVE, {}, Expected(None, None, (), {})),
    "call.playback.ended": (LIVE, {}, Expected(None, None, (), {})),
    "streaming.started": (LIVE, {}, Expected(None, None, (), {"streaming": "1"})),
    "streaming.stopped": (LIVE, {}, Expected(None, None, (), {"streaming": "0"})),
    "streaming.failed": (
        LIVE,
        {"failure_reason": "connection_failed"},
        Expected(None, None, (EventType.CALL_ERROR,), {"streaming": "0"}),
    ),
    "conference.created": (LIVE, {}, Expected(None, None, (), {"conference_id": "conf-1"})),
    "conference.participant.joined": (LIVE, {}, Expected(None, None, (), {})),
    "conference.participant.left": (LIVE, {}, Expected(None, None, (), {})),
    "conference.ended": (LIVE, {}, Expected(None, None, (), {})),
    **{name: (LIVE, {}, Expected(None, None, (), {}, publish=False)) for name in STORED_ONLY_TYPES},
}


def test_cases_cover_every_type() -> None:
    assert set(CASES) == VOICE_EVENT_TYPES


@pytest.mark.parametrize("event_type", sorted(CASES))
def test_effects_of_every_event_type(event_type: str) -> None:
    snapshot, fields, expected = CASES[event_type]
    effects = plan(event_type, snapshot, **fields)
    assert (effects.leg.state if effects.leg else None) == expected.leg
    call = effects.call
    assert (None if call is None else (call.to or "fields")) == expected.call
    assert tuple(event.type for event in effects.events) == expected.events
    assert effects.carrier_state == expected.state
    assert effects.publish is expected.publish
    assert effects.signal is (event_type in SIGNALLED_TYPES)
    assert (effects.amd_result is not None) is (event_type in AMD_DETECTION_TYPES)


def test_answered_records_times_on_leg_and_call() -> None:
    effects = plan("call.answered")
    assert effects.leg is not None
    assert effects.call is not None
    assert effects.leg.values == {"answered_at": AT}
    assert effects.call.values == {"answered_at": AT, "started_at": AT}


def test_transfer_target_answer_touches_only_its_leg() -> None:
    effects = plan("call.answered", replace(LIVE, leg_role="transfer_target"))
    assert effects.call is None
    assert effects.events == ()
    assert effects.carrier_state == {"transfer_target_answered": "1"}
    assert effects.signal


@pytest.mark.parametrize("event_type", ["call.bridged", "conference.participant.joined"])
def test_transfer_target_join_completes_the_transfer(event_type: str) -> None:
    effects = plan(event_type, replace(LIVE, status="transferring", leg_role="transfer_target"))
    assert effects.call is not None
    assert effects.call.to == "transferred"
    assert "transferring" in allowed_from("transferred")
    assert [event.type for event in effects.events] == [EventType.CALL_TRANSFERRED]
    assert effects.carrier_state == {"bridged": "1", "transfer_started_at": AT.isoformat()}
    if event_type == "call.bridged":
        assert effects.call.values == {"transferred_to": "+14155550123"}


def test_transfer_target_hangup_leaves_the_call_alone() -> None:
    effects = plan("call.hangup", replace(LIVE, leg_role="transfer_target"), hangup_cause="normal_clearing")
    assert effects.leg is not None
    assert effects.leg.state == "hangup"
    assert effects.call is None
    assert effects.carrier_state == {"transfer_target_hangup": "1"}


def test_hangup_leg_values_are_stored_verbatim() -> None:
    effects = plan(
        "call.hangup", LIVE, hangup_cause="brand_new_cause", hangup_source="caller", sip_hangup_cause=499
    )
    assert effects.leg is not None
    assert effects.leg.values == {
        "hangup_cause": "brand_new_cause",
        "hangup_source": "caller",
        "sip_hangup_cause": "499",
        "ended_at": AT,
    }


def hangup(snapshot: CallSnapshot, **fields: Any) -> tuple[CallStatus | None, dict[str, Any]]:
    model = CallHangup.model_validate({"call_control_id": CCID, **fields})
    outcome = hangup_outcome(model, snapshot, AT)
    if outcome.change is None:
        return None, {}
    return outcome.change.to, outcome.change.values


NEVER_ANSWERED: Final[list[tuple[dict[str, Any], str, str, dict[str, Any]]]] = [
    (
        {"hangup_cause": "timeout"},
        "no_answer",
        "no_answer",
        {"answered_by": "no_answer", "disposition_tag": "NO_ANSWER"},
    ),
    (
        {"hangup_cause": "no_answer"},
        "no_answer",
        "no_answer",
        {"answered_by": "no_answer", "disposition_tag": "NO_ANSWER"},
    ),
    ({"hangup_cause": "user_busy"}, "busy", "busy", {"disposition_tag": "BUSY"}),
    ({"hangup_cause": "call_rejected"}, "failed", "rejected", {"disposition_tag": "FAILED"}),
    ({"hangup_cause": "not_found"}, "failed", "invalid_number", {}),
    ({"hangup_cause": "normal_clearing", "sip_hangup_cause": "404"}, "failed", "invalid_number", {}),
    ({"hangup_cause": "unspecified", "sip_hangup_cause": "484"}, "failed", "invalid_number", {}),
    ({"hangup_cause": "unspecified", "sip_hangup_cause": "604"}, "failed", "invalid_number", {}),
    ({"hangup_cause": "originator_cancel"}, "canceled", "canceled", {}),
    (
        {"hangup_cause": "normal_clearing", "sip_hangup_cause": "480"},
        "failed",
        "carrier_error",
        {"error_message": "Carrier hangup: normal_clearing (480)"},
    ),
    (
        {"hangup_cause": "time_limit"},
        "failed",
        "carrier_error",
        {"error_message": "Carrier hangup: time_limit (unknown)"},
    ),
    (
        {"hangup_cause": "something_new", "sip_hangup_cause": "503"},
        "failed",
        "carrier_error",
        {"error_message": "Carrier hangup: something_new (503)"},
    ),
    ({}, "failed", "carrier_error", {"error_message": "Carrier hangup: unknown (unknown)"}),
]


@pytest.mark.parametrize(("fields", "status", "reason", "extra"), NEVER_ANSWERED)
def test_hangup_before_answer_follows_the_cause_table(
    fields: dict[str, Any], status: str, reason: str, extra: dict[str, Any]
) -> None:
    for snapshot in (replace(SNAPSHOT, status="dialing"), SNAPSHOT):
        to, values = hangup(snapshot, **fields)
        assert to is not None
        assert to == status
        assert status in {"no_answer", "busy", "failed", "canceled"}
        assert snapshot.status in allowed_from(to)
        assert values == {"end_reason": reason, "ended_by": END_REASONS[reason], "ended_at": AT, **extra}  # type: ignore[index]


def test_hangup_end_time_wins_over_occurred_at() -> None:
    end = AT - timedelta(seconds=2)
    _, values = hangup(LIVE, hangup_cause="normal_clearing", end_time=end.isoformat())
    assert values["ended_at"] == end
    assert values["connected_seconds"] == Decimal("18.000")


@pytest.mark.parametrize(
    ("snapshot", "fields", "reason"),
    [
        (LIVE, {"hangup_cause": "normal_clearing", "hangup_source": "callee"}, "user_hangup"),
        (LIVE, {"hangup_cause": "normal_clearing", "hangup_source": "unknown"}, "user_hangup"),
        (LIVE, {"hangup_cause": "time_limit"}, "max_duration"),
        (replace(LIVE, status="transferring"), {"hangup_cause": "normal_clearing"}, "user_hangup"),
    ],
)
def test_answered_hangup_without_owner_completes_the_call(
    snapshot: CallSnapshot, fields: dict[str, Any], reason: str
) -> None:
    to, values = hangup(snapshot, **fields)
    assert to == "completed"
    assert values == {
        "end_reason": reason,
        "ended_by": END_REASONS[reason],  # type: ignore[index]
        "ended_at": AT,
        "connected_seconds": Decimal("20.000"),
    }


def test_answered_hangup_after_transfer_splits_the_billing_clocks() -> None:
    bridged = ANSWERED + timedelta(seconds=7, milliseconds=250)
    to, values = hangup(
        replace(LIVE, status="transferred", transfer_started_at=bridged), hangup_cause="normal_clearing"
    )
    assert to == "completed"
    assert values["end_reason"] == "transferred"
    assert values["ended_by"] == "system"
    assert values["connected_seconds"] == Decimal("7.250")
    assert values["transfer_seconds"] == Decimal("12.750")


@pytest.mark.parametrize(
    "snapshot",
    [replace(LIVE, owner_present=True), replace(LIVE, end_reason="agent_end_call")],
    ids=["pipeline-running", "we-hung-up"],
)
def test_answered_hangup_owned_elsewhere_changes_nothing(snapshot: CallSnapshot) -> None:
    assert hangup(snapshot, hangup_cause="normal_clearing", hangup_source="callee") == (None, {})
    effects = plan("call.hangup", snapshot, hangup_cause="normal_clearing")
    assert effects.call is None
    assert effects.events == ()
    assert effects.leg is not None
    assert effects.signal


def test_hangup_event_carries_end_reason_and_ended_by() -> None:
    effects = plan("call.hangup", replace(SNAPSHOT, status="dialing"), hangup_cause="user_busy")
    assert [(event.type, event.data) for event in effects.events] == [
        (EventType.CALL_ENDED, {"end_reason": "busy", "ended_by": "system"})
    ]


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("human", "human_residence"),
        ("machine", "machine"),
        ("not_sure", "unknown"),
        ("silence", "silence"),
        (None, "unknown"),
    ],
)
def test_standard_amd_results_are_normalized(raw: str | None, normalized: str) -> None:
    assert normalize_amd_result(raw) == normalized
    effects = plan("call.machine.detection.ended", LIVE, result=raw)
    assert effects.amd_result == (raw or "unknown")
    assert [event.data for event in effects.events] == [{"result": normalized}]
    assert effects.call is None


@pytest.mark.parametrize(
    ("raw", "answered_by"),
    [
        ("human_residence", "human"),
        ("human_business", "human"),
        ("machine", "voicemail"),
        ("silence", "unknown"),
        ("fax_detected", "fax"),
        ("brand_new", "unknown"),
    ],
)
def test_premium_amd_sets_answered_by(raw: str, answered_by: str) -> None:
    assert premium_answered_by(raw) == answered_by
    effects = plan("call.machine.premium.detection.ended", LIVE, result=raw)
    assert effects.call is not None
    assert (effects.call.to, effects.call.values) == (None, {"answered_by": answered_by})
    assert [event.data for event in effects.events] == [{"result": raw, "answered_by": answered_by}]
    assert effects.amd_result == raw


@pytest.mark.parametrize(
    ("event_type", "flag"),
    [
        ("call.machine.greeting.ended", "greeting_ended"),
        ("call.machine.premium.greeting.ended", "beep_detected"),
        ("call.machine.premium.call_screening.detected", "screening"),
    ],
)
def test_amd_flags(event_type: str, flag: str) -> None:
    assert [event.data for event in plan(event_type, LIVE).events] == [{flag: True}]


@pytest.mark.parametrize(
    ("event_type", "fields", "code", "level"),
    [
        ("call.recording.error", {"reason": "upload_failed"}, "recording_failed", "warning"),
        ("streaming.failed", {"failure_reason": "connection_failed"}, "stream_failed", "error"),
    ],
)
def test_error_events_carry_the_code(event_type: str, fields: dict[str, Any], code: str, level: str) -> None:
    (event,) = plan(event_type, LIVE, **fields).events
    assert event.data == {"code": code, "detail": next(iter(fields.values()))}
    assert event.level == level


def test_unknown_type_is_a_key_error() -> None:
    with pytest.raises(KeyError):
        plan_effects("call.unknown", payload("call.answered"), SNAPSHOT, occurred_at=AT)


def test_wrong_payload_model_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="expected a CallHangup payload"):
        plan_effects("call.hangup", payload("call.answered"), LIVE, occurred_at=AT)
