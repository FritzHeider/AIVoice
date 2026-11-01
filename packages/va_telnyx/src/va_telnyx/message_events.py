"""Telnyx messaging webhook payload models (specs/21-messaging-sms.md §5; specs/06-telephony-telnyx.md §4.1
Decision P03-T09 rule 3).

``message.received``, ``message.sent``, and ``message.finalized`` carry a full ``message`` record: the fields
of :class:`va_telnyx.Message` (``from_`` is the sender's number, ``to[0].status`` the delivery status) plus
media, errors, cost, and timestamps.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Final

from va_telnyx.message_models import Message
from va_telnyx.models import ResponseModel

__all__ = [
    "MESSAGE_PAYLOAD_MODELS",
    "MessageCost",
    "MessageErrorItem",
    "MessageEventPayload",
    "MessageMedia",
]


class MessageErrorItem(ResponseModel):
    code: str
    title: str | None = None
    detail: str | None = None


class MessageMedia(ResponseModel):
    url: str
    content_type: str | None = None


class MessageCost(ResponseModel):
    amount: Decimal | None = None
    currency: str | None = None


class MessageEventPayload(Message):
    """The ``message`` record of a messaging webhook."""

    record_type: str | None = None
    subject: str | None = None
    media: tuple[MessageMedia, ...] = ()
    errors: tuple[MessageErrorItem, ...] = ()
    cost: MessageCost | None = None
    webhook_url: str | None = None
    received_at: datetime | None = None
    sent_at: datetime | None = None
    completed_at: datetime | None = None


MESSAGE_PAYLOAD_MODELS: Final[Mapping[str, type[MessageEventPayload]]] = {
    "message.received": MessageEventPayload,
    "message.sent": MessageEventPayload,
    "message.finalized": MessageEventPayload,
}
