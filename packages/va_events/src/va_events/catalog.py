"""The event type catalog (specs/19-events-and-webhooks.md §2) and stream routing (§1 step 2).

``EventType`` holds every type of the §2 table; ``DEFAULT_CATEGORY`` mirrors its Category column (``—`` is
``None``). ``tests/test_catalog.py`` parses the spec table and compares both, so a new row in §2 needs a
member and a category entry here.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal

from va_db.models import CallEventCategory

__all__ = [
    "ALWAYS_CALL_WEBHOOK",
    "DEFAULT_CATEGORY",
    "STREAMS",
    "WEBHOOK_KEY",
    "CallEventCategory",
    "EventType",
    "Stream",
    "stream_for",
]

Stream = Literal["calls", "sms", "widget", "billing", "system"]
STREAMS: Final[tuple[Stream, ...]] = ("calls", "sms", "widget", "billing", "system")


class EventType(StrEnum):
    """Every domain event type, by its dotted name."""

    CALL_QUEUED = "call.queued"
    CALL_SCHEDULED = "call.scheduled"
    CALL_DISPATCH_BLOCKED = "call.dispatch_blocked"
    CALL_DIALING = "call.dialing"
    CALL_RINGING = "call.ringing"
    CALL_ANSWERED = "call.answered"
    CALL_AMD = "call.amd"
    CALL_VOICEMAIL = "call.voicemail"
    CALL_STARTED = "call.started"
    CALL_TURN = "call.turn"
    CALL_INTERRUPT = "call.interrupt"
    CALL_LATENCY = "call.latency"
    CALL_ROUTE = "call.route"
    CALL_EXTRACTION = "call.extraction"
    CALL_LOOP_CONDITION = "call.loop_condition"
    CALL_TOOL = "call.tool"
    CALL_DYNAMIC_DATA = "call.dynamic_data"
    CALL_WEBHOOK_NODE = "call.webhook_node"
    CALL_WEBHOOK_DELIVERY = "call.webhook_delivery"
    CALL_GUARD_RAIL = "call.guard_rail"
    CALL_TRANSFER_STARTED = "call.transfer_started"
    CALL_TRANSFERRED = "call.transferred"
    CALL_POST_TRANSFER_TRANSCRIPT = "call.post_transfer_transcript"
    CALL_SMS_SENT = "call.sms_sent"
    CALL_ERROR = "call.error"
    CALL_ENDED = "call.ended"
    CALL_RECORDING_READY = "call.recording_ready"
    CALL_ANALYSIS_READY = "call.analysis_ready"
    CALL_COMPLETED = "call.completed"
    CITATIONS_READY = "citations.ready"
    EVALS_COMPLETED = "evals.completed"
    BATCH_CREATED = "batch.created"
    BATCH_VALIDATING = "batch.validating"
    BATCH_DISPATCHING = "batch.dispatching"
    BATCH_IN_PROGRESS = "batch.in_progress"
    BATCH_COMPLETED = "batch.completed"
    BATCH_STOPPED = "batch.stopped"
    BATCH_FAILED = "batch.failed"
    SMS_RECEIVED = "sms.received"
    SMS_SENT = "sms.sent"
    SMS_DELIVERED = "sms.delivered"
    SMS_FAILED = "sms.failed"
    CONVERSATION_CREATED = "conversation.created"
    CONVERSATION_MESSAGE = "conversation.message"
    CONVERSATION_CLOSED = "conversation.closed"
    CONVERSATION_HUMAN_TAKEOVER = "conversation.human_takeover"
    WIDGET_MESSAGE = "widget.message"
    BILLING_LOW_BALANCE = "billing.low_balance"
    BILLING_RECHARGED = "billing.recharged"
    BILLING_PAYMENT_FAILED = "billing.payment_failed"
    NUMBER_PURCHASED = "number.purchased"
    NUMBER_RELEASED = "number.released"
    NUMBER_ORDER_FAILED = "number.order_failed"
    TENDLC_BRAND_STATUS = "tendlc.brand_status"
    TENDLC_CAMPAIGN_STATUS = "tendlc.campaign_status"
    CALL_RETRANSCRIPTION_READY = "call.retranscription_ready"
    CALL_TRANSCRIPT_CORRECTED = "call.transcript_corrected"
    VOICE_CLONE_READY = "voice.clone_ready"
    VOICE_CLONE_FAILED = "voice.clone_failed"
    SYSTEM_FRAUD_SUSPECTED = "system.fraud_suspected"
    SYSTEM_RETENTION_COMPLETED = "system.retention_completed"
    KB_READY = "kb.ready"
    KB_FAILED = "kb.failed"


_CATEGORIES: dict[str, CallEventCategory] = {
    "call.queued": "queue",
    "call.scheduled": "queue",
    "call.dispatch_blocked": "queue",
    "call.dialing": "call",
    "call.ringing": "call",
    "call.answered": "call",
    "call.amd": "voicemail",
    "call.voicemail": "voicemail",
    "call.started": "call",
    "call.turn": "call",
    "call.interrupt": "interrupt",
    "call.latency": "latency",
    "call.route": "route",
    "call.extraction": "extraction",
    "call.loop_condition": "route",
    "call.tool": "tool",
    "call.dynamic_data": "dynamic_data",
    "call.webhook_node": "webhook",
    "call.webhook_delivery": "webhook",
    "call.guard_rail": "guard_rail",
    "call.transfer_started": "transfer",
    "call.transferred": "transfer",
    "call.post_transfer_transcript": "post_transfer_transcript",
    "call.sms_sent": "sms",
    "call.error": "error",
    "call.ended": "call",
    "call.recording_ready": "call",
    "call.analysis_ready": "call",
    "call.completed": "call",
    "citations.ready": "citations",
    "evals.completed": "evals",
    "sms.received": "sms",
    "sms.sent": "sms",
    "sms.delivered": "sms",
    "sms.failed": "sms",
    "call.retranscription_ready": "call",
    "call.transcript_corrected": "call",
}

DEFAULT_CATEGORY: Final[Mapping[EventType, CallEventCategory | None]] = MappingProxyType(
    {member: _CATEGORIES.get(member.value) for member in EventType}
)

_WEBHOOK_KEYS: dict[str, str] = {
    "call.queued": "queue",
    "call.scheduled": "queue",
    "call.dispatch_blocked": "queue",
    "call.dialing": "call",
    "call.ringing": "call",
    "call.answered": "call",
    "call.amd": "call",
    "call.voicemail": "call",
    "call.started": "call",
    "call.turn": "call",
    "call.interrupt": "call",
    "call.latency": "latency",
    "call.route": "call",
    "call.extraction": "call",
    "call.loop_condition": "call",
    "call.tool": "tool",
    "call.dynamic_data": "dynamic_data",
    "call.webhook_node": "webhook",
    "call.guard_rail": "call",
    "call.transfer_started": "call",
    "call.transferred": "call",
    "call.post_transfer_transcript": "post_transfer_transcript",
    "call.sms_sent": "call",
    "call.error": "call",
    "call.ended": "call",
    "call.recording_ready": "call",
    "citations.ready": "citations",
    "evals.completed": "evals",
    "call.retranscription_ready": "call",
    "call.transcript_corrected": "call",
}

WEBHOOK_KEY: Final[Mapping[EventType, str | None]] = MappingProxyType(
    {member: _WEBHOOK_KEYS.get(member.value) for member in EventType}
)
"""The per-call ``webhook_events`` key of each type (§2 last column); ``None`` = never sent live."""

ALWAYS_CALL_WEBHOOK: Final[frozenset[EventType]] = frozenset({EventType.CALL_COMPLETED})
"""Types every per-call ``webhook`` receives whatever its ``webhook_events`` (the Call Object, §3 rule 1)."""

_STREAM_BY_PREFIX: Final[Mapping[str, Stream]] = MappingProxyType(
    {
        "call": "calls",
        "batch": "calls",
        "citations": "calls",
        "evals": "calls",
        "sms": "sms",
        "conversation": "sms",
        "widget": "widget",
        "billing": "billing",
    }
)


def stream_for(event_type: EventType) -> Stream:
    """The ``events:<stream>`` of an event type (specs/19 §1 step 2; ``system`` for other prefixes)."""
    prefix = event_type.value.partition(".")[0]
    return _STREAM_BY_PREFIX.get(prefix, "system")
