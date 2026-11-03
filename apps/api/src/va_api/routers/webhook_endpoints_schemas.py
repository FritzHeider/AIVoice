"""Bodies of ``/v1/webhooks/endpoints`` and ``/v1/webhooks/deliveries`` (specs/19-events-and-webhooks.md §10).

Rules: specs/19 §10 Decision P06-T04b. ``event_types`` entries are ``*``, an exact catalog event type, or
``<prefix>.*`` where some catalog type starts with ``<prefix>.``. Delivery views never carry the ``payload``
(size and PHI); ``next_attempt_at`` is shown only while a delivery is ``pending``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Final

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue

from va_db.models import WebhookDelivery, WebhookEndpoint
from va_db.models.billing import WebhookDeliveryStatus, WebhookFormat
from va_events import EventType

__all__ = [
    "DUPLICATE_EVENT_TYPES_MESSAGE",
    "MAX_URL_LENGTH",
    "UNKNOWN_EVENT_TYPE_MESSAGE",
    "WebhookDeliveryView",
    "WebhookEndpointCreate",
    "WebhookEndpointDeleted",
    "WebhookEndpointPatch",
    "WebhookEndpointView",
    "WebhookTestRequest",
    "WebhookTestResult",
    "audited_endpoint",
    "delivery_view",
    "endpoint_view",
    "valid_event_type_pattern",
]

MAX_URL_LENGTH: Final = 2048
MAX_DESCRIPTION_LENGTH: Final = 500
MAX_EVENT_TYPES: Final = 100
UNKNOWN_EVENT_TYPE_MESSAGE: Final = "must be *, a known event type, or <prefix>.* matching known event types."
DUPLICATE_EVENT_TYPES_MESSAGE: Final = "event_types must not repeat an entry."
UNKNOWN_TEST_EVENT_MESSAGE: Final = "must be a known event type."

_CATALOG: Final = frozenset(member.value for member in EventType)
_WILDCARD: Final = "*"
_PREFIX_SUFFIX: Final = ".*"

_ENDPOINT_EXAMPLE: Final[dict[str, JsonValue]] = {
    "id": "0192f3a4-5b6c-7d8e-9f01-23456789abcd",
    "url": "https://hooks.acme.test/voiceagents",
    "description": "CRM sync",
    "event_types": ["call.completed", "sms.*"],
    "enabled": True,
    "phi_allowed": False,
    "created_at": "2026-11-03T09:30:00Z",
    "updated_at": "2026-11-03T09:30:00Z",
}
_CREATE_EXAMPLE: Final[dict[str, JsonValue]] = {
    "url": "https://hooks.acme.test/voiceagents",
    "description": "CRM sync",
    "event_types": ["call.completed", "sms.*"],
}
_PATCH_EXAMPLE: Final[dict[str, JsonValue]] = {"enabled": False}
_TEST_REQUEST_EXAMPLE: Final[dict[str, JsonValue]] = {"event_type": "call.completed"}
_TEST_RESULT_EXAMPLE: Final[dict[str, JsonValue]] = {
    "event_id": "evt_0192f3a45b6c7d8e9f0123456789abcd",
    "event_type": "call.completed",
    "succeeded": True,
    "status_code": 200,
    "error": None,
    "response_ms": 142,
}
_DELIVERY_EXAMPLE: Final[dict[str, JsonValue]] = {
    "id": "0192f3a4-5b6c-7d8e-9f01-23456789abce",
    "event_id": "evt_0192f3a45b6c7d8e9f0123456789abcd",
    "event_type": "call.completed",
    "url": "https://hooks.acme.test/voiceagents",
    "endpoint_id": "0192f3a4-5b6c-7d8e-9f01-23456789abcd",
    "format": "envelope",
    "status": "dead",
    "attempts": 9,
    "next_attempt_at": None,
    "last_status_code": 500,
    "last_error": "http_status",
    "last_response_ms": 88,
    "call_id": "0192f3a4-5b6c-7d8e-9f01-23456789abcf",
    "batch_id": None,
    "conversation_id": None,
    "delivered_at": None,
    "created_at": "2026-11-02T09:30:00Z",
    "updated_at": "2026-11-03T00:41:12Z",
}


def valid_event_type_pattern(pattern: str) -> bool:
    """Whether ``pattern`` is ``*``, a catalog event type, or ``<prefix>.*`` matching some catalog type."""
    if pattern == _WILDCARD or pattern in _CATALOG:
        return True
    if not pattern.endswith(_PREFIX_SUFFIX):
        return False
    prefix = pattern.removesuffix(_WILDCARD)
    return len(prefix) > 1 and _WILDCARD not in prefix and any(name.startswith(prefix) for name in _CATALOG)


def _event_type_pattern(value: str) -> str:
    if not valid_event_type_pattern(value):
        raise ValueError(UNKNOWN_EVENT_TYPE_MESSAGE)
    return value


def _unique(values: list[str]) -> list[str]:
    if len(set(values)) != len(values):
        raise ValueError(DUPLICATE_EVENT_TYPES_MESSAGE)
    return values


def _test_event_type(value: str) -> str:
    if value not in _CATALOG:
        raise ValueError(UNKNOWN_TEST_EVENT_MESSAGE)
    return value


type EndpointUrl = Annotated[str, Field(min_length=1, max_length=MAX_URL_LENGTH)]
type Description = Annotated[str, Field(max_length=MAX_DESCRIPTION_LENGTH)]
type EventTypes = Annotated[
    list[Annotated[str, AfterValidator(_event_type_pattern)]],
    Field(min_length=1, max_length=MAX_EVENT_TYPES),
    AfterValidator(_unique),
]


class WebhookEndpointCreate(BaseModel):
    """A new org webhook endpoint."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [_CREATE_EXAMPLE]})

    url: EndpointUrl = Field(
        description="Absolute http(s) URL; https outside dev unless insecure is allowed."
    )
    description: Description = Field(default="", description="Free text shown in the dashboard.")
    event_types: EventTypes = Field(
        default_factory=lambda: [_WILDCARD],
        description="`*`, exact event types, or `<prefix>.*` globs (default every event).",
    )
    enabled: bool = Field(default=True, description="Disabled endpoints receive no deliveries.")
    phi_allowed: bool = Field(
        default=False,
        description="Under HIPAA mode only flagged endpoints get transcripts (needs `org.compliance`).",
    )


class WebhookEndpointPatch(BaseModel):
    """Any subset of an endpoint's fields; fields left out keep their value."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [_PATCH_EXAMPLE]})

    url: EndpointUrl = Field(default="", description="New URL; deliveries already queued keep the old one.")
    description: Description = Field(default="")
    event_types: EventTypes = Field(default_factory=lambda: [_WILDCARD])
    enabled: bool = Field(default=True, description="Re-enabling an auto-disabled endpoint is allowed.")
    phi_allowed: bool = Field(default=False)


class WebhookEndpointView(BaseModel):
    """An org webhook endpoint."""

    model_config = ConfigDict(json_schema_extra={"examples": [_ENDPOINT_EXAMPLE]})

    id: uuid.UUID
    url: str
    description: str
    event_types: list[str]
    enabled: bool
    phi_allowed: bool
    created_at: datetime
    updated_at: datetime


class WebhookEndpointDeleted(BaseModel):
    """The endpoint is gone; its queued deliveries were cancelled."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"deleted": True}]})

    deleted: bool


class WebhookTestRequest(BaseModel):
    """Which event type the synthetic test event claims to be."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [_TEST_REQUEST_EXAMPLE]})

    event_type: Annotated[str, AfterValidator(_test_event_type)] = Field(
        default=EventType.CALL_COMPLETED.value, description="A catalog event type (default `call.completed`)."
    )


class WebhookTestResult(BaseModel):
    """The outcome of the single, unrecorded test delivery."""

    model_config = ConfigDict(json_schema_extra={"examples": [_TEST_RESULT_EXAMPLE]})

    event_id: str
    event_type: str
    succeeded: bool
    status_code: int | None = Field(description="HTTP status of the response, when one arrived.")
    error: str | None = Field(description="Delivery error code (specs/19 §8), `null` on success.")
    response_ms: int | None


class WebhookDeliveryView(BaseModel):
    """One outbox row: an event sent (or to be sent) to one URL."""

    model_config = ConfigDict(json_schema_extra={"examples": [_DELIVERY_EXAMPLE]})

    id: uuid.UUID
    event_id: str
    event_type: str
    url: str
    endpoint_id: uuid.UUID | None
    format: WebhookFormat
    status: WebhookDeliveryStatus
    attempts: int
    next_attempt_at: datetime | None = Field(
        description="When the next attempt is due; only while `pending`."
    )
    last_status_code: int | None
    last_error: str | None
    last_response_ms: int | None
    call_id: uuid.UUID | None
    batch_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    delivered_at: datetime | None
    created_at: datetime
    updated_at: datetime


def endpoint_view(row: WebhookEndpoint) -> WebhookEndpointView:
    return WebhookEndpointView(
        id=row.id,
        url=row.url,
        description=row.description,
        event_types=list(row.event_types),
        enabled=row.enabled,
        phi_allowed=row.phi_allowed,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def audited_endpoint(row: WebhookEndpoint) -> dict[str, Any]:
    """The endpoint as a JSON-safe audit value (no timestamps)."""
    return endpoint_view(row).model_dump(mode="json", exclude={"created_at", "updated_at"})


def delivery_view(row: WebhookDelivery) -> WebhookDeliveryView:
    return WebhookDeliveryView(
        id=row.id,
        event_id=row.event_id,
        event_type=row.event_type,
        url=row.url,
        endpoint_id=row.endpoint_id,
        format=row.format,
        status=row.status,
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at if row.status == "pending" else None,
        last_status_code=row.last_status_code,
        last_error=row.last_error,
        last_response_ms=row.last_response_ms,
        call_id=row.call_id,
        batch_id=row.batch_id,
        conversation_id=row.conversation_id,
        delivered_at=row.delivered_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
