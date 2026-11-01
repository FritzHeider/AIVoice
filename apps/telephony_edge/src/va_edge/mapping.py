"""Telnyx voice event → domain effects (specs/06-telephony-telnyx.md §5, §5.1 and the §5 Decision P05-T02).

:func:`plan_effects` is pure: given the event type, its parsed payload, and a :class:`CallSnapshot` of the
resolved call, it returns the :class:`CarrierEffects` the handler (:mod:`va_edge.carrier`) applies: the leg
and call transitions (enforced again by the guarded repository updates), the domain events, the
``call:{<id>}:carrier_state`` fields, and whether the event goes to the carrier channel and the workflow.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from pydantic import JsonValue

from va_db.call_states import END_REASONS, EndReason, call_durations
from va_db.models import AnsweredBy, CallEventLevel, CallLegState, CallStatus
from va_domain.carrier import CarrierLeg
from va_events.catalog import EventType
from va_telnyx.voice_events import (
    CallBridged,
    CallHangup,
    CallPayload,
    ConferencePayload,
    RecordingError,
    StreamingFailed,
    VoicePayload,
)

__all__ = [
    "AMD_DETECTION_TYPES",
    "SIGNALLED_TYPES",
    "STORED_ONLY_TYPES",
    "VOICE_EVENT_TYPES",
    "CallChange",
    "CallSnapshot",
    "CarrierEffects",
    "HangupOutcome",
    "LegChange",
    "PlannedEvent",
    "hangup_outcome",
    "normalize_amd_result",
    "plan_effects",
    "premium_answered_by",
]

STORED_ONLY_TYPES: Final = frozenset(
    {
        "call.hold",
        "call.unhold",
        "call.deepfake_detection.result",
        "call.deepfake_detection.error",
        "call.conversation.created",
        "call.conversation.start_failed",
    }
)
"""Types that are stored and nothing else: no carrier channel message, no signal."""

AMD_DETECTION_TYPES: Final = frozenset(
    {"call.machine.detection.ended", "call.machine.premium.detection.ended"}
)
"""Types that carry an AMD verdict (counted in ``va_amd_results_total``)."""

SIGNALLED_TYPES: Final = frozenset(
    {
        "call.answered",
        "call.bridged",
        "call.hangup",
        "call.machine.detection.ended",
        "call.machine.greeting.ended",
        "call.machine.premium.detection.ended",
        "call.machine.premium.greeting.ended",
        "call.machine.premium.call_screening.detected",
        "streaming.failed",
        "call.recording.saved",
        "call.recording.error",
    }
)
"""Types signalled to ``CallWorkflow`` (specs/06 §5 Decision P05-T02 item 3)."""

_INVALID_SIP_CAUSES: Final = frozenset({"404", "484", "604"})
_STANDARD_AMD: Final[Mapping[str, str]] = {
    "human": "human_residence",
    "machine": "machine",
    "not_sure": "unknown",
}
_PREMIUM_ANSWERED_BY: Final[Mapping[str, AnsweredBy]] = {
    "human_residence": "human",
    "human_business": "human",
    "machine": "voicemail",
    "silence": "unknown",
    "fax_detected": "fax",
}
_DISPOSITIONS: Final[Mapping[str, str]] = {"no_answer": "NO_ANSWER", "busy": "BUSY", "rejected": "FAILED"}


@dataclass(frozen=True, slots=True)
class CallSnapshot:
    """What the mapping needs to know about the resolved call before the event is applied."""

    call_id: uuid.UUID
    org_id: uuid.UUID
    status: CallStatus
    leg_role: CarrierLeg
    answered_at: datetime | None = None
    end_reason: str | None = None
    owner_present: bool = False
    transfer_started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class LegChange:
    """Move the event's leg to ``state`` (guarded) and set ``values``."""

    state: CallLegState
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CallChange:
    """Move the call to ``to`` (guarded) setting ``values``; ``to=None`` sets ``values`` only."""

    to: CallStatus | None
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlannedEvent:
    """A domain event to publish for the call."""

    type: EventType
    data: dict[str, JsonValue] = field(default_factory=dict)
    level: CallEventLevel = "info"


@dataclass(frozen=True, slots=True)
class CarrierEffects:
    """Everything one carrier event changes. ``events`` of a call status change are published only when the
    change applied (Decision P05-T02 item 8)."""

    leg: LegChange | None = None
    call: CallChange | None = None
    events: tuple[PlannedEvent, ...] = ()
    carrier_state: dict[str, str] = field(default_factory=dict)
    amd_result: str | None = None
    publish: bool = True
    signal: bool = False


@dataclass(frozen=True, slots=True)
class HangupOutcome:
    """The call change a primary-leg hangup causes, or ``None`` when someone else ends the call."""

    change: CallChange | None
    end_reason: EndReason | None = None


def normalize_amd_result(result: str | None) -> str:
    """Standard AMD result in the premium vocabulary (unknown values verbatim, missing → ``unknown``)."""
    if result is None:
        return "unknown"
    return _STANDARD_AMD.get(result, result)


def premium_answered_by(result: str | None) -> AnsweredBy:
    """``calls.answered_by`` of a premium AMD result (anything unmapped → ``unknown``)."""
    return _PREMIUM_ANSWERED_BY.get(result or "", "unknown")


def _iso(value: datetime) -> str:
    return value.isoformat()


def _ended(reason: EndReason, ended_at: datetime, **values: Any) -> dict[str, Any]:
    return {"end_reason": reason, "ended_by": END_REASONS[reason], "ended_at": ended_at, **values}


def _never_answered(payload: CallHangup) -> tuple[CallStatus, EndReason, dict[str, Any]]:
    cause = payload.hangup_cause
    if cause in {"timeout", "no_answer"}:
        return "no_answer", "no_answer", {"answered_by": "no_answer"}
    if cause == "user_busy":
        return "busy", "busy", {}
    if cause == "call_rejected":
        return "failed", "rejected", {}
    if cause == "not_found" or payload.sip_hangup_cause in _INVALID_SIP_CAUSES:
        return "failed", "invalid_number", {}
    if cause == "originator_cancel":
        return "canceled", "canceled", {}
    sip = payload.sip_hangup_cause or "unknown"
    return "failed", "carrier_error", {"error_message": f"Carrier hangup: {cause or 'unknown'} ({sip})"}


def hangup_outcome(payload: CallHangup, snapshot: CallSnapshot, occurred_at: datetime) -> HangupOutcome:
    """specs/06 §5.1 for the primary leg."""
    ended_at = payload.end_time or occurred_at
    if snapshot.answered_at is None:
        status, failure, extra = _never_answered(payload)
        disposition = _DISPOSITIONS.get(failure)
        if disposition is not None:
            extra["disposition_tag"] = disposition
        return HangupOutcome(CallChange(status, _ended(failure, ended_at, **extra)), failure)
    if snapshot.end_reason is not None or snapshot.owner_present:
        return HangupOutcome(None)
    reason: EndReason = "user_hangup"
    if payload.hangup_cause == "time_limit":
        reason = "max_duration"
    elif snapshot.status == "transferred":
        reason = "transferred"
    values = call_durations(snapshot.answered_at, ended_at, snapshot.transfer_started_at)
    return HangupOutcome(CallChange("completed", _ended(reason, ended_at, **values)), reason)


type _Planner = Callable[[VoicePayload, CallSnapshot, datetime], CarrierEffects]


def _as[P: CallPayload | ConferencePayload](payload: VoicePayload, model: type[P]) -> P:
    if not isinstance(payload, model):
        msg = f"expected a {model.__name__} payload, got {type(payload).__name__}"
        raise TypeError(msg)
    return payload


def _initiated(_payload: VoicePayload, _snapshot: CallSnapshot, _at: datetime) -> CarrierEffects:
    return CarrierEffects(
        leg=LegChange("initiated"),
        call=CallChange("ringing"),
        events=(PlannedEvent(EventType.CALL_RINGING),),
    )


def _answered(_payload: VoicePayload, snapshot: CallSnapshot, at: datetime) -> CarrierEffects:
    leg = LegChange("answered", {"answered_at": at})
    if snapshot.leg_role != "primary":
        return CarrierEffects(leg=leg, carrier_state={f"{snapshot.leg_role}_answered": "1"}, signal=True)
    return CarrierEffects(
        leg=leg,
        call=CallChange("in_progress", {"answered_at": at, "started_at": at}),
        events=(PlannedEvent(EventType.CALL_ANSWERED),),
        carrier_state={"answered": "1", "answered_at": _iso(at)},
        signal=True,
    )


def _transfer_joined(payload: VoicePayload, at: datetime, leg: LegChange | None) -> CarrierEffects:
    values: dict[str, Any] = {}
    if isinstance(payload, CallPayload) and payload.to is not None:
        values["transferred_to"] = payload.to
    return CarrierEffects(
        leg=leg,
        call=CallChange("transferred", values),
        events=(PlannedEvent(EventType.CALL_TRANSFERRED),),
        carrier_state={"bridged": "1", "transfer_started_at": _iso(at)},
        signal=True,
    )


def _bridged(payload: VoicePayload, snapshot: CallSnapshot, at: datetime) -> CarrierEffects:
    leg = LegChange("bridged")
    if snapshot.leg_role == "transfer_target":
        return _transfer_joined(_as(payload, CallBridged), at, leg)
    return CarrierEffects(leg=leg, carrier_state={f"{snapshot.leg_role}_bridged": "1"}, signal=True)


def _hangup(payload: VoicePayload, snapshot: CallSnapshot, at: datetime) -> CarrierEffects:
    hangup = _as(payload, CallHangup)
    leg = LegChange(
        "hangup",
        {
            "hangup_cause": hangup.hangup_cause,
            "hangup_source": hangup.hangup_source,
            "sip_hangup_cause": hangup.sip_hangup_cause,
            "ended_at": hangup.end_time or at,
        },
    )
    state = {f"{snapshot.leg_role}_hangup": "1"}
    if snapshot.leg_role != "primary":
        return CarrierEffects(leg=leg, carrier_state=state, signal=True)
    if hangup.hangup_cause is not None:
        state["hangup_cause"] = hangup.hangup_cause
    outcome = hangup_outcome(hangup, snapshot, at)
    events: tuple[PlannedEvent, ...] = ()
    if outcome.change is not None and outcome.end_reason is not None:
        data: dict[str, JsonValue] = {
            "end_reason": outcome.end_reason,
            "ended_by": END_REASONS[outcome.end_reason],
        }
        events = (PlannedEvent(EventType.CALL_ENDED, data),)
    return CarrierEffects(leg=leg, call=outcome.change, events=events, carrier_state=state, signal=True)


def _result_of(payload: VoicePayload) -> str | None:
    value = getattr(payload, "result", None)
    return value if isinstance(value, str) else None


def _amd_standard(payload: VoicePayload, _snapshot: CallSnapshot, _at: datetime) -> CarrierEffects:
    raw = _result_of(payload)
    result = normalize_amd_result(raw)
    return CarrierEffects(
        events=(PlannedEvent(EventType.CALL_AMD, {"result": result}),),
        carrier_state={"amd_result": result},
        amd_result=raw or "unknown",
        signal=True,
    )


def _amd_premium(payload: VoicePayload, _snapshot: CallSnapshot, _at: datetime) -> CarrierEffects:
    raw = _result_of(payload) or "unknown"
    answered_by = premium_answered_by(raw)
    return CarrierEffects(
        call=CallChange(None, {"answered_by": answered_by}),
        events=(PlannedEvent(EventType.CALL_AMD, {"result": raw, "answered_by": answered_by}),),
        carrier_state={"amd_result": raw, "answered_by": answered_by},
        amd_result=raw,
        signal=True,
    )


def _amd_flag(flag: str, state: str) -> _Planner:
    def plan(_payload: VoicePayload, _snapshot: CallSnapshot, _at: datetime) -> CarrierEffects:
        return CarrierEffects(
            events=(PlannedEvent(EventType.CALL_AMD, {flag: True}),), carrier_state={state: "1"}, signal=True
        )

    return plan


def _recording_saved(_payload: VoicePayload, _snapshot: CallSnapshot, _at: datetime) -> CarrierEffects:
    return CarrierEffects(signal=True)


def _recording_error(payload: VoicePayload, _snapshot: CallSnapshot, _at: datetime) -> CarrierEffects:
    reason = _as(payload, RecordingError).reason
    data: dict[str, JsonValue] = {"code": "recording_failed", "detail": reason or ""}
    return CarrierEffects(events=(PlannedEvent(EventType.CALL_ERROR, data, "warning"),), signal=True)


def _streaming(value: str) -> _Planner:
    def plan(_payload: VoicePayload, _snapshot: CallSnapshot, _at: datetime) -> CarrierEffects:
        return CarrierEffects(carrier_state={"streaming": value})

    return plan


def _streaming_failed(payload: VoicePayload, _snapshot: CallSnapshot, _at: datetime) -> CarrierEffects:
    reason = _as(payload, StreamingFailed).failure_reason
    data: dict[str, JsonValue] = {"code": "stream_failed", "detail": reason or ""}
    return CarrierEffects(
        events=(PlannedEvent(EventType.CALL_ERROR, data, "error"),),
        carrier_state={"streaming": "0"},
        signal=True,
    )


def _conference_created(payload: VoicePayload, _snapshot: CallSnapshot, _at: datetime) -> CarrierEffects:
    return CarrierEffects(carrier_state={"conference_id": _as(payload, ConferencePayload).conference_id})


def _participant_joined(payload: VoicePayload, snapshot: CallSnapshot, at: datetime) -> CarrierEffects:
    if snapshot.leg_role == "transfer_target":
        return _transfer_joined(payload, at, None)
    return CarrierEffects()


def _forward(_payload: VoicePayload, _snapshot: CallSnapshot, _at: datetime) -> CarrierEffects:
    return CarrierEffects()


def _stored(_payload: VoicePayload, _snapshot: CallSnapshot, _at: datetime) -> CarrierEffects:
    return CarrierEffects(publish=False)


_PLANNERS: Final[Mapping[str, _Planner]] = {
    "call.initiated": _initiated,
    "call.answered": _answered,
    "call.bridged": _bridged,
    "call.hangup": _hangup,
    "call.machine.detection.ended": _amd_standard,
    "call.machine.greeting.ended": _amd_flag("greeting_ended", "greeting_ended"),
    "call.machine.premium.detection.ended": _amd_premium,
    "call.machine.premium.greeting.ended": _amd_flag("beep_detected", "beep_detected"),
    "call.machine.premium.call_screening.detected": _amd_flag("screening", "screening"),
    "call.dtmf.received": _forward,
    "call.recording.saved": _recording_saved,
    "call.recording.error": _recording_error,
    "call.playback.started": _forward,
    "call.playback.ended": _forward,
    "streaming.started": _streaming("1"),
    "streaming.stopped": _streaming("0"),
    "streaming.failed": _streaming_failed,
    "conference.created": _conference_created,
    "conference.participant.joined": _participant_joined,
    "conference.participant.left": _forward,
    "conference.ended": _forward,
    **dict.fromkeys(STORED_ONLY_TYPES, _stored),
}

VOICE_EVENT_TYPES: Final = frozenset(_PLANNERS)
"""Every voice event type of the specs/06 §5 table (the handler is registered for each)."""


def plan_effects(
    event_type: str, payload: VoicePayload, snapshot: CallSnapshot, *, occurred_at: datetime
) -> CarrierEffects:
    """The effects of one voice event on the resolved call; ``KeyError`` for a type outside the §5 table."""
    return _PLANNERS[event_type](payload, snapshot, occurred_at)
