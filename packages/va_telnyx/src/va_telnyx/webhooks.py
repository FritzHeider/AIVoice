"""Telnyx webhook envelope and payload dispatch (specs/06-telephony-telnyx.md §4.1 step 5, §5; specs/06 §4.1
Decision P03-T09 rule 3).

Envelope: ``{"data": {"record_type": "event", "id", "event_type", "occurred_at", "payload"}, "meta":
{"attempt", "delivered_to"}}``. Parse it only after :func:`va_telnyx.signatures.verify_webhook` accepted the
raw bytes. :func:`parse_payload` returns the typed model of a known ``event_type`` and an
:class:`UnknownEventPayload` (raw payload kept) for any other type, which is never an error: the edge stores
it and counts ``telnyx_webhook_unhandled_total``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import orjson
from pydantic import BaseModel, ConfigDict, ValidationError

from va_telnyx.message_events import MESSAGE_PAYLOAD_MODELS, MessageEventPayload
from va_telnyx.voice_events import VOICE_PAYLOAD_MODELS, CallPayload, ConferencePayload, VoicePayload

__all__ = [
    "MESSAGING_EVENT_TYPES",
    "PAYLOAD_MODELS",
    "VOICE_EVENT_TYPES",
    "EventPayload",
    "UnknownEventPayload",
    "WebhookEnvelope",
    "WebhookEvent",
    "WebhookMeta",
    "WebhookParseError",
    "parse_envelope",
    "parse_payload",
]


class WebhookParseError(ValueError):
    """A verified webhook body that is not a Telnyx event envelope, or a known event with a malformed
    payload."""


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, coerce_numbers_to_str=True)


class WebhookMeta(_Envelope):
    attempt: int | None = None
    delivered_to: str | None = None


class WebhookEvent(_Envelope):
    record_type: str = "event"
    id: str
    event_type: str
    occurred_at: datetime
    payload: dict[str, Any] = {}


class WebhookEnvelope(_Envelope):
    data: WebhookEvent
    meta: WebhookMeta | None = None


@dataclass(frozen=True, slots=True)
class UnknownEventPayload:
    """The payload of an ``event_type`` we have no model for."""

    event_type: str
    raw: dict[str, Any]


type EventPayload = VoicePayload | MessageEventPayload | UnknownEventPayload

type _PayloadModel = type[CallPayload] | type[ConferencePayload] | type[MessageEventPayload]

PAYLOAD_MODELS: Final[Mapping[str, _PayloadModel]] = {**VOICE_PAYLOAD_MODELS, **MESSAGE_PAYLOAD_MODELS}
VOICE_EVENT_TYPES: Final = frozenset(VOICE_PAYLOAD_MODELS)
MESSAGING_EVENT_TYPES: Final = frozenset(MESSAGE_PAYLOAD_MODELS)


def parse_envelope(raw_body: bytes) -> WebhookEnvelope:
    """The envelope of a verified webhook body."""
    try:
        return WebhookEnvelope.model_validate(orjson.loads(raw_body))
    except (orjson.JSONDecodeError, ValidationError) as exc:
        msg = f"the webhook body is not a Telnyx event envelope: {type(exc).__name__}"
        raise WebhookParseError(msg) from exc


def parse_payload(event: WebhookEvent) -> EventPayload:
    """The typed payload of ``event`` (:class:`UnknownEventPayload` for an unknown ``event_type``)."""
    model = PAYLOAD_MODELS.get(event.event_type)
    if model is None:
        return UnknownEventPayload(event.event_type, dict(event.payload))
    try:
        parsed: EventPayload = model.model_validate(event.payload)
    except ValidationError as exc:
        msg = f"the {event.event_type} payload is malformed: {exc.error_count()} error(s)"
        raise WebhookParseError(msg) from exc
    return parsed
