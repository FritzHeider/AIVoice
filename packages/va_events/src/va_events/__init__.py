"""VoiceAgents events: the domain event bus (specs/19-events-and-webhooks.md §1-§2) and the ClickHouse
analytics schema (specs/03-data-model.md §12)."""

from importlib.metadata import version

from va_events.buffer import CallEventBuffer
from va_events.call_events import (
    CallEventRow,
    CallEventSink,
    CallEventWriteError,
    CallEventWriter,
    DbCallEventWriter,
    DirectCallEventWriter,
    insert_rows,
    row_for,
)
from va_events.catalog import DEFAULT_CATEGORY, STREAMS, EventType, Stream, stream_for
from va_events.consumer import StreamConsumer, StreamEntry
from va_events.model import DomainEvent, EventFields, decode, encode, event_uuid, is_call_event, new_event
from va_events.publisher import EventPublisher, EventPublishError, WebhookRouter

__all__ = [
    "DEFAULT_CATEGORY",
    "STREAMS",
    "CallEventBuffer",
    "CallEventRow",
    "CallEventSink",
    "CallEventWriteError",
    "CallEventWriter",
    "DbCallEventWriter",
    "DirectCallEventWriter",
    "DomainEvent",
    "EventFields",
    "EventPublishError",
    "EventPublisher",
    "EventType",
    "Stream",
    "StreamConsumer",
    "StreamEntry",
    "WebhookRouter",
    "__version__",
    "decode",
    "encode",
    "event_uuid",
    "insert_rows",
    "is_call_event",
    "new_event",
    "row_for",
    "stream_for",
]

__version__ = version("va-events")
