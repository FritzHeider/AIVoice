"""VoiceAgents events: the domain event bus (specs/19-events-and-webhooks.md §1-§2) and the ClickHouse
analytics schema (specs/03-data-model.md §12)."""

from importlib.metadata import version

from va_events.buffer import CallEventBuffer
from va_events.call_ended import ENDED_MESSAGE, call_ended_event
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
from va_events.call_messages import QUEUED_MESSAGE, SCHEDULED_MESSAGE
from va_events.catalog import (
    ALWAYS_CALL_WEBHOOK,
    DEFAULT_CATEGORY,
    INTERNAL_TYPES,
    STREAMS,
    WEBHOOK_KEY,
    EventType,
    Stream,
    stream_for,
)
from va_events.consumer import StreamConsumer, StreamEntry
from va_events.dead_letter import DeadLetter
from va_events.live_hub import LiveHub, LiveHubError, LiveSubscription, LiveSubscriptionClosedError
from va_events.model import DomainEvent, EventFields, decode, encode, event_uuid, is_call_event, new_event
from va_events.publisher import EventPublisher, EventPublishError, WebhookRouter
from va_events.stream_tail import StreamTailError, StreamTailHub, TailSubscription
from va_events.subscription import Subscription, SubscriptionClosedError
from va_events.usage import UsageEventData, usage_event
from va_events.webhook_payloads import (
    BatchFacts,
    batch_body,
    envelope_body,
    iso_ms,
    live_body,
    row_body,
    strip_phi,
)
from va_events.webhook_router import DbWebhookRouter

__all__ = [
    "ALWAYS_CALL_WEBHOOK",
    "DEFAULT_CATEGORY",
    "ENDED_MESSAGE",
    "INTERNAL_TYPES",
    "QUEUED_MESSAGE",
    "SCHEDULED_MESSAGE",
    "STREAMS",
    "WEBHOOK_KEY",
    "BatchFacts",
    "CallEventBuffer",
    "CallEventRow",
    "CallEventSink",
    "CallEventWriteError",
    "CallEventWriter",
    "DbCallEventWriter",
    "DbWebhookRouter",
    "DeadLetter",
    "DirectCallEventWriter",
    "DomainEvent",
    "EventFields",
    "EventPublishError",
    "EventPublisher",
    "EventType",
    "LiveHub",
    "LiveHubError",
    "LiveSubscription",
    "LiveSubscriptionClosedError",
    "Stream",
    "StreamConsumer",
    "StreamEntry",
    "StreamTailError",
    "StreamTailHub",
    "Subscription",
    "SubscriptionClosedError",
    "TailSubscription",
    "UsageEventData",
    "WebhookRouter",
    "__version__",
    "batch_body",
    "call_ended_event",
    "decode",
    "encode",
    "envelope_body",
    "event_uuid",
    "insert_rows",
    "is_call_event",
    "iso_ms",
    "live_body",
    "new_event",
    "row_body",
    "row_for",
    "stream_for",
    "strip_phi",
    "usage_event",
]

__version__ = version("va-events")
