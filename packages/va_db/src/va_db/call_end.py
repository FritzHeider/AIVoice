"""Recording the end of an answered call (specs/05-calls-api.md §8, §9; specs/07 §18 Decision P05-T11b).

Both the media worker's end-of-call routine and the ``end_call`` activity of ``CallWorkflow`` move an answered
call (``in_progress``, ``transferring``, ``transferred``) to ``completed`` through
:func:`record_answered_end`: an ``end_reason`` already stored on the row wins over the caller's reason (so a
late writer never overwrites the first recorded end), ``ended_by`` follows the reason (specs/05 §9), and the
connected/transfer seconds come from :func:`va_db.call_states.call_durations`. A voicemail end
(``voicemail_hangup``, ``voicemail_left``) also sets ``disposition_tag='VOICEMAIL'`` unless the call already
has a disposition (specs/07 §14 Decision P05-T10c).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from va_db.call_states import END_REASONS, EndReason, call_durations
from va_db.models import Call, CallStatus
from va_db.repositories import CallRepo

__all__ = [
    "ANSWERED_STATUSES",
    "VOICEMAIL_DISPOSITION",
    "VOICEMAIL_REASONS",
    "ended_values",
    "record_answered_end",
    "stored_reason",
]

ANSWERED_STATUSES: Final[frozenset[CallStatus]] = frozenset({"in_progress", "transferring", "transferred"})
"""The statuses an answered call that has not ended yet may be in."""

VOICEMAIL_REASONS: Final[frozenset[EndReason]] = frozenset({"voicemail_hangup", "voicemail_left"})
"""End reasons that tag the call ``VOICEMAIL`` when it has no disposition yet (specs/07 §14 Decision
P05-T10c)."""

VOICEMAIL_DISPOSITION: Final = "VOICEMAIL"


def stored_reason(value: str | None) -> EndReason | None:
    """``calls.end_reason`` as an :data:`EndReason` (``None`` when unset or not a known reason)."""
    for reason in END_REASONS:
        if reason == value:
            return reason
    return None


def ended_values(reason: EndReason, ended_at: datetime, error_message: str | None) -> dict[str, Any]:
    """The ``calls`` columns of an end: reason, ``ended_by``, ``ended_at`` and (when given) the message."""
    values: dict[str, Any] = {"end_reason": reason, "ended_by": END_REASONS[reason], "ended_at": ended_at}
    if error_message is not None:
        values["error_message"] = error_message
    return values


async def record_answered_end(  # noqa: PLR0913 - the call plus keyword-only end facts
    session: AsyncSession,
    call: Call,
    reason: EndReason,
    *,
    ended_at: datetime,
    transfer_started_at: datetime | None = None,
    error_message: str | None = None,
) -> tuple[Call, EndReason]:
    """Move the answered ``call`` to ``completed`` in ``session``'s transaction; returns the updated row and
    the reason recorded (the stored one when the row already had a known reason). A call that is not in an
    answered status raises :class:`va_db.repositories.CallTransitionError`."""
    recorded = stored_reason(call.end_reason) or reason
    values = ended_values(recorded, ended_at, call.error_message or error_message)
    if recorded in VOICEMAIL_REASONS and call.disposition_tag is None:
        values["disposition_tag"] = VOICEMAIL_DISPOSITION
    if call.answered_at is not None:
        values.update(call_durations(call.answered_at, ended_at, transfer_started_at))
    ended = await CallRepo(session, call.org_id).transition(
        call.id, "completed", from_=ANSWERED_STATUSES, **values
    )
    return ended, recorded
