"""Call events written by the api in its own transaction (specs/05 §5 Decision P05-T05b).

:func:`persist_event` assigns the event's ``seq`` in Redis, writes its ``call_events`` row, and routes it
to the webhook outbox (specs/19 §1 Decision P06-T07) in the caller's transaction; after the commit
:func:`broadcast_event` sends it to the stream and the live channel (best effort: the row is durable).
"""

from __future__ import annotations

from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from va_common.errors import AppError
from va_common.logging import get_logger
from va_events import DomainEvent, EventPublisher, EventPublishError, insert_rows, row_for

__all__ = ["EVENTS_UNAVAILABLE_MESSAGE", "broadcast_event", "persist_event"]

EVENTS_UNAVAILABLE_MESSAGE: Final = "Call events are temporarily unavailable."

_log = get_logger(__name__)


async def persist_event(session: AsyncSession, publisher: EventPublisher, event: DomainEvent) -> DomainEvent:
    """Assign the seq (Redis) and write the ``call_events`` row in ``session``'s transaction; a Redis failure
    is ``SERVICE_UNAVAILABLE``."""
    try:
        sequenced = await publisher.sequence(event)
    except EventPublishError as exc:
        _log.warning("calls.event_sequence_failed", event_type=event.type.value, call_id=str(event.call_id))
        raise AppError("SERVICE_UNAVAILABLE", EVENTS_UNAVAILABLE_MESSAGE) from exc
    row = row_for(sequenced)
    await insert_rows(session, [] if row is None else [row])
    await publisher.route([sequenced], session=session)
    return sequenced


async def broadcast_event(publisher: EventPublisher, event: DomainEvent) -> None:
    """Send a committed event to the stream and live channel; a Redis failure is logged, not raised."""
    try:
        await publisher.broadcast(event)
    except EventPublishError:
        _log.warning("calls.event_broadcast_failed", event_type=event.type.value, call_id=str(event.call_id))
