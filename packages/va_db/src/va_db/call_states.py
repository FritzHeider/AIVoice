"""Call and call-leg state machines (specs/05-calls-api.md §8, §9 and the §8 Decision P05-T02).

Each transition table maps a target state to the states it may be entered from; a state that appears as no
source is terminal. ``va_db.repositories.CallRepo.transition`` enforces :data:`CALL_TRANSITIONS` with a
guarded ``UPDATE ... WHERE status = ANY(:allowed_from)``, so a carrier redelivery is a ``CONFLICT``, never a
regression.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

from va_db.models import CallLegState, CallStatus, EndedBy

__all__ = [
    "CALL_TRANSITIONS",
    "END_REASONS",
    "LEG_TRANSITIONS",
    "TERMINAL_STATUSES",
    "EndReason",
    "allowed_from",
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

_RINGING_PHASE: Final[frozenset[CallStatus]] = frozenset({"dialing", "ringing"})

CALL_TRANSITIONS: Final[Mapping[CallStatus, frozenset[CallStatus]]] = MappingProxyType(
    {
        "scheduled": frozenset(),
        "queued": frozenset({"scheduled"}),
        "dialing": frozenset({"queued"}),
        "ringing": frozenset({"dialing"}),
        # ``dialing`` too: Telnyx may deliver ``call.answered`` before ``call.initiated``.
        "in_progress": _RINGING_PHASE,
        "transferring": frozenset({"in_progress"}),
        "transferred": frozenset({"transferring"}),
        "completed": frozenset({"in_progress", "transferring", "transferred"}),
        "failed": frozenset({"queued", "dialing", "ringing"}),
        "no_answer": _RINGING_PHASE,
        "busy": _RINGING_PHASE,
        "canceled": frozenset({"scheduled", "queued", "dialing", "ringing"}),
    }
)
"""Target status → allowed source statuses."""

TERMINAL_STATUSES: Final[frozenset[CallStatus]] = frozenset(
    {"completed", "failed", "canceled", "no_answer", "busy"}
)

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
