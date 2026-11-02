"""``call.ended``: the event every writer of a call end emits (specs/19-events-and-webhooks.md §2;
specs/07 §18 Decision P05-T11b).

The telephony edge, the ``end_call`` activity and the media worker's end-of-call routine all emit it with the
data ``{end_reason, ended_by}`` of the end they recorded.
"""

from __future__ import annotations

import uuid
from typing import Final

from va_common.time import Clock
from va_db.call_states import END_REASONS, EndReason
from va_events.catalog import EventType
from va_events.model import DomainEvent, new_event

__all__ = ["ENDED_MESSAGE", "call_ended_event"]

ENDED_MESSAGE: Final = "Call ended."


def call_ended_event(clock: Clock, org_id: uuid.UUID, call_id: uuid.UUID, reason: EndReason) -> DomainEvent:
    """A new ``call.ended`` event of the call (``occurred_at`` from ``clock``)."""
    return new_event(
        EventType.CALL_ENDED,
        org_id=org_id,
        clock=clock,
        call_id=call_id,
        level="info",
        message=ENDED_MESSAGE,
        data={"end_reason": reason, "ended_by": END_REASONS[reason]},
    )
