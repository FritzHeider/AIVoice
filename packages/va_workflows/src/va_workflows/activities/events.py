"""Call events of the activities: written with the state change they describe, broadcast after the commit.

:func:`persist_events` assigns each event's per-call ``seq`` in Redis, writes its ``call_events`` row, and
routes it to the webhook outbox (the publisher's router, specs/19 §1 Decision P06-T07) in the caller's open
transaction; a Redis failure raises a retryable ``EventsUnavailable`` so the whole
transaction (status change included) rolls back and the activity retry repeats it. :func:`broadcast_events`
sends the committed events to the stream and live channel; best effort, because the rows are durable.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.logging import get_logger
from va_common.time import Clock
from va_db.models import CallEventLevel
from va_events import (
    ENDED_MESSAGE,
    DomainEvent,
    EventPublisher,
    EventPublishError,
    EventType,
    call_ended_event,
    insert_rows,
    new_event,
    row_for,
)
from va_workflows.activities.errors import EVENTS_UNAVAILABLE, retryable

__all__ = ["ENDED_MESSAGE", "broadcast_events", "call_event", "ended_event", "persist_events"]

EVENTS_UNAVAILABLE_MESSAGE = "Call events are temporarily unavailable."

_log = get_logger(__name__)


async def persist_events(
    session: AsyncSession, publisher: EventPublisher, events: Sequence[DomainEvent]
) -> list[DomainEvent]:
    """The sequenced events, their rows inserted and deliveries routed in ``session``'s transaction."""
    sequenced: list[DomainEvent] = []
    for event in events:
        try:
            sequenced.append(await publisher.sequence(event))
        except EventPublishError as exc:
            _log.warning("calls.event_sequence_failed", event_type=event.type.value)
            raise retryable(EVENTS_UNAVAILABLE_MESSAGE, EVENTS_UNAVAILABLE) from exc
    await insert_rows(session, [row for event in sequenced if (row := row_for(event)) is not None])
    await publisher.route(sequenced, session=session)
    return sequenced


async def broadcast_events(publisher: EventPublisher, events: Sequence[DomainEvent]) -> None:
    """Broadcast committed events in order; a Redis failure is logged and the rest are still tried."""
    for event in events:
        try:
            await publisher.broadcast(event)
        except EventPublishError:
            _log.warning("calls.event_broadcast_failed", event_type=event.type.value)


def call_event(  # noqa: PLR0913 - the event's identity and keyword-only content
    clock: Clock,
    org_id: uuid.UUID,
    call_id: uuid.UUID,
    event_type: EventType,
    *,
    message: str,
    data: dict[str, JsonValue],
    level: CallEventLevel = "info",
) -> DomainEvent:
    """A new event of the call (``occurred_at`` from ``clock``)."""
    return new_event(
        event_type, org_id=org_id, clock=clock, call_id=call_id, level=level, message=message, data=data
    )


ended_event = call_ended_event
"""``call.ended`` with ``{end_reason, ended_by}`` (the edge's shape; shared with the media worker)."""
