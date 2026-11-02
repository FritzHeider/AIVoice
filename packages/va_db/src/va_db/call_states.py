"""Call and call-leg state machines (specs/05-calls-api.md §8, §9 and the §8 Decision P05-T02).

Each transition table maps a target state to the states it may be entered from; a state that appears as no
source is terminal. ``va_db.repositories.CallRepo.transition`` enforces :data:`CALL_TRANSITIONS` with a
guarded ``UPDATE ... WHERE status = ANY(:allowed_from)``, so a carrier redelivery is a ``CONFLICT``, never a
regression.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Final, Literal

from va_db.models import CallLegState, CallStatus, EndedBy

__all__ = [
    "ACTIVE_STATUSES",
    "CALL_TRANSITIONS",
    "DURATION_PRECISION",
    "END_REASONS",
    "LEG_TRANSITIONS",
    "RINGING_PHASE",
    "STOPPABLE_STATUSES",
    "TERMINAL_STATUSES",
    "EndReason",
    "allowed_from",
    "call_durations",
    "is_terminal",
    "leg_allowed_from",
]

EndReason = Literal[
    "agent_end_call",
    "user_hangup",
    "max_duration",
    "no_input_timeout",
    "voicemail_hangup",
    "voicemail_left",
    "api_stop",
    "guard_rail_end",
    "transferred",
    "warm_transferred",
    "transfer_failed_end",
    "no_answer",
    "busy",
    "rejected",
    "invalid_number",
    "carrier_error",
    "canceled",
    "dispatch_blocked",
    "insufficient_credits",
    "worker_lost",
    "worker_crash",
    "error",
    "ivr_dead_end",
    "fax_detected",
    "inbound_rejected",
    "loop_protection",
]

END_REASONS: Final[Mapping[EndReason, EndedBy]] = MappingProxyType(
    {
        "agent_end_call": "agent",
        "user_hangup": "user",
        "max_duration": "timeout",
        "no_input_timeout": "system",
        "voicemail_hangup": "system",
        "voicemail_left": "agent",
        "api_stop": "api",
        "guard_rail_end": "guard_rail",
        "transferred": "system",
        "warm_transferred": "system",
        "transfer_failed_end": "system",
        "no_answer": "system",
        "busy": "system",
        "rejected": "system",
        "invalid_number": "system",
        "carrier_error": "error",
        "canceled": "api",
        "dispatch_blocked": "system",
        "insufficient_credits": "system",
        "worker_lost": "error",
        "worker_crash": "error",
        "error": "error",
        "ivr_dead_end": "system",
        "fax_detected": "system",
        "inbound_rejected": "system",
        "loop_protection": "system",
    }
)
"""``end_reason`` → ``ended_by`` (specs/05 §9, in table order)."""

RINGING_PHASE: Final[frozenset[CallStatus]] = frozenset({"dialing", "ringing"})
"""The statuses before the answer; the carrier's primary ``call.answered`` moves a call to ``in_progress``
only from these."""

CALL_TRANSITIONS: Final[Mapping[CallStatus, frozenset[CallStatus]]] = MappingProxyType(
    {
        "scheduled": frozenset(),
        "queued": frozenset({"scheduled"}),
        "dialing": frozenset({"queued"}),
        "ringing": frozenset({"dialing"}),
        # ``dialing`` too: Telnyx may deliver ``call.answered`` before ``call.initiated``.
        # ``transferring`` too: a failed cold transfer resumes the call (specs/05 §8 Decision P05-T13).
        "in_progress": RINGING_PHASE | {"transferring"},
        "transferring": frozenset({"in_progress"}),
        "transferred": frozenset({"transferring"}),
        "completed": frozenset({"in_progress", "transferring", "transferred"}),
        # ``scheduled`` too: its workflow could not be started (specs/05 §5 Decision P05-T05b).
        "failed": frozenset({"scheduled", "queued", "dialing", "ringing"}),
        "no_answer": RINGING_PHASE,
        "busy": RINGING_PHASE,
        "canceled": frozenset({"scheduled", "queued", "dialing", "ringing"}),
    }
)
"""Target status → allowed source statuses."""

TERMINAL_STATUSES: Final[frozenset[CallStatus]] = frozenset(
    {"completed", "failed", "canceled", "no_answer", "busy"}
)

ACTIVE_STATUSES: Final[frozenset[CallStatus]] = frozenset(
    {"dialing", "ringing", "in_progress", "transferring"}
)
"""The statuses ``GET /v1/calls/active`` lists (specs/05 §7.4 Decision P05-T16b): the carrier leg exists and
the AI still owns the call."""

STOPPABLE_STATUSES: Final[frozenset[CallStatus]] = frozenset(
    {"scheduled", "queued", "dialing", "ringing", "in_progress", "transferring"}
)
"""The statuses ``POST /v1/calls/{id}/stop`` acts on (specs/05 §7.3 Decision P05-T11c): every status that is
neither terminal nor ``transferred`` (the AI has left a transferred call)."""

LEG_TRANSITIONS: Final[Mapping[CallLegState, frozenset[CallLegState]]] = MappingProxyType(
    {
        "initiated": frozenset(),
        "ringing": frozenset({"initiated"}),
        "answered": frozenset({"initiated", "ringing"}),
        # A bridge implies an answer; ``call.bridged`` may overtake ``call.answered``.
        "bridged": frozenset({"initiated", "ringing", "answered"}),
        "hangup": frozenset({"initiated", "ringing", "answered", "bridged"}),
    }
)
"""Target leg state → allowed source leg states (``initiated`` is only ever inserted)."""


def allowed_from(to: CallStatus) -> frozenset[CallStatus]:
    """The statuses a call may move to ``to`` from."""
    return CALL_TRANSITIONS[to]


def leg_allowed_from(to: CallLegState) -> frozenset[CallLegState]:
    """The leg states a leg may move to ``to`` from."""
    return LEG_TRANSITIONS[to]


def is_terminal(status: CallStatus) -> bool:
    """Whether ``status`` ends the call's status machine (no outgoing transition)."""
    return status in TERMINAL_STATUSES


DURATION_PRECISION: Final = Decimal("0.001")
"""``calls.connected_seconds`` / ``transfer_seconds`` are stored with millisecond precision."""


def _seconds(start: datetime, end: datetime) -> Decimal:
    return max(Decimal(0), Decimal(str((end - start).total_seconds()))).quantize(DURATION_PRECISION)


def call_durations(
    answered_at: datetime, ended_at: datetime, transfer_started_at: datetime | None = None
) -> dict[str, Decimal]:
    """``connected_seconds`` (answer → end, or → the transfer start when one began first) and, after a
    transfer started, ``transfer_seconds`` (transfer start → end); negative spans clamp to 0 (specs/06 §5.1).
    """
    connected_end = ended_at
    values: dict[str, Decimal] = {}
    if transfer_started_at is not None:
        connected_end = min(ended_at, transfer_started_at)
        values["transfer_seconds"] = _seconds(transfer_started_at, ended_at)
    values["connected_seconds"] = _seconds(answered_at, connected_end)
    return values
