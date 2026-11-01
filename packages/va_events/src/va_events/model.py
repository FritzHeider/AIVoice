"""The internal domain event (specs/19-events-and-webhooks.md §1) and its wire encoding.

A stream entry and a live pub/sub message carry the same bytes: the orjson of ``model_dump(mode="json")``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, TypedDict, Unpack

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue

from va_common.ids import new_id
from va_common.json import dumps
from va_common.time import Clock, ensure_utc
from va_db.models import CallEventCategory, CallEventLevel
from va_events.catalog import EventType

__all__ = ["DomainEvent", "EventFields", "decode", "encode", "event_uuid", "is_call_event", "new_event"]

_EVENT_ID_PATTERN = r"^evt_[0-9a-f]{32}$"
_ID_PREFIX = "evt_"


class DomainEvent(BaseModel):
    """One domain event; ``seq`` is assigned by the publisher for call events."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Annotated[str, Field(pattern=_EVENT_ID_PATTERN)]
    type: EventType
    org_id: uuid.UUID
    occurred_at: Annotated[datetime, AfterValidator(ensure_utc)]
    call_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    widget_id: uuid.UUID | None = None
    category: CallEventCategory | None = None
    level: CallEventLevel = "info"
    message: str = ""
    data: dict[str, JsonValue] = Field(default_factory=dict)
    seq: int | None = None


class EventFields(TypedDict, total=False):
    """The optional :class:`DomainEvent` fields :func:`new_event` accepts."""

    call_id: uuid.UUID | None
    batch_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    widget_id: uuid.UUID | None
    category: CallEventCategory | None
    level: CallEventLevel
    message: str
    data: dict[str, JsonValue]


def new_event(
    event_type: EventType, *, org_id: uuid.UUID, clock: Clock, **fields: Unpack[EventFields]
) -> DomainEvent:
    """A new event with a fresh ``evt_<uuid7 hex>`` id, ``occurred_at`` = ``clock.now()``, no ``seq``."""
    return DomainEvent(
        id=_ID_PREFIX + new_id().hex, type=event_type, org_id=org_id, occurred_at=clock.now(), **fields
    )


def event_uuid(event: DomainEvent) -> uuid.UUID:
    """The uuid inside ``evt_<hex>`` (the ``call_events.id`` of the event's row)."""
    return uuid.UUID(hex=event.id.removeprefix(_ID_PREFIX))


def is_call_event(event: DomainEvent) -> bool:
    """Whether the event belongs to a call (``call_id`` set): only these get a ``seq`` and live publishing."""
    return event.call_id is not None


def encode(event: DomainEvent) -> bytes:
    """The event's wire bytes (stream ``payload`` field and live channel message)."""
    return dumps(event.model_dump(mode="json"))


def decode(payload: bytes | str) -> DomainEvent:
    """Parse wire bytes back into an event (``pydantic.ValidationError`` when they are not one)."""
    return DomainEvent.model_validate_json(payload)
