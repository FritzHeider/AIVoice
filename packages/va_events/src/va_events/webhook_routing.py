"""Webhook routing (specs/19-events-and-webhooks.md §3): which URLs an event goes to, in which format, with
which body. Pure: the database router (:mod:`va_events.webhook_router`) gathers the :class:`RoutingFacts`.

Targets are planned in a fixed order and deduplicated by URL (``webhook_deliveries`` is unique per event and
URL; the first planned target wins): per-call ``webhook`` → batch ``status_webhook`` → conversation
``webhook`` → widget ``webhook_url`` → org endpoints (in their stored order).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Final, Literal

from pydantic import JsonValue

from va_db.models import WebhookFormat
from va_events.catalog import ALWAYS_CALL_WEBHOOK, WEBHOOK_KEY, EventType
from va_events.model import DomainEvent
from va_events.webhook_payloads import BatchFacts, Body, content_body, envelope_body, strip_phi

__all__ = [
    "CallFacts",
    "DeliveryPolicy",
    "EndpointTarget",
    "PlannedDelivery",
    "RoutingFacts",
    "Target",
    "TargetKind",
    "matches",
    "plan_deliveries",
    "plan_targets",
    "reaches_batch",
    "reaches_conversation",
    "reaches_widget",
    "render_deliveries",
    "wants",
]

TargetKind = Literal["call", "batch", "conversation", "widget", "endpoint"]

_BATCH_PREFIX: Final = "batch."
_CONVERSATION_PREFIXES: Final = ("sms.", "conversation.")
_WIDGET_PREFIXES: Final = ("conversation.",)


@dataclass(frozen=True, slots=True)
class CallFacts:
    """The call's per-call webhook settings (``calls.config``) and its ``metadata``."""

    webhook: str | None
    webhook_events: frozenset[str] = frozenset()
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EndpointTarget:
    """An enabled org webhook endpoint."""

    id: uuid.UUID
    url: str
    event_types: tuple[str, ...]
    phi_allowed: bool = False


@dataclass(frozen=True, slots=True)
class RoutingFacts:
    """Everything routing needs to know about an event's call, batch, conversation, widget, and org."""

    call: CallFacts | None = None
    batch: BatchFacts | None = None
    batch_url: str | None = None
    conversation_url: str | None = None
    widget_url: str | None = None
    endpoints: tuple[EndpointTarget, ...] = ()


@dataclass(frozen=True, slots=True)
class DeliveryPolicy:
    """Org settings that shape bodies: ``hipaa_mode`` and the per-call ``webhook_format``."""

    hipaa: bool = False
    call_format: WebhookFormat = "bland"


@dataclass(frozen=True, slots=True)
class Target:
    """One destination of an event."""

    kind: TargetKind
    url: str
    endpoint_id: uuid.UUID | None = None
    phi_allowed: bool = False


@dataclass(frozen=True, slots=True)
class PlannedDelivery:
    """One ``webhook_deliveries`` row to insert."""

    url: str
    format: WebhookFormat
    payload: Body
    endpoint_id: uuid.UUID | None = None


def wants(event: DomainEvent, call: CallFacts) -> bool:
    """Whether the per-call webhook receives ``event`` (§3 rule 1)."""
    if event.type in ALWAYS_CALL_WEBHOOK:
        return True
    key = WEBHOOK_KEY[event.type]
    return key is not None and key in call.webhook_events


def matches(event_type: EventType, patterns: Iterable[str]) -> bool:
    """Whether ``event_type`` matches any glob of an endpoint's ``event_types`` (``*`` = every type)."""
    return any(fnmatchcase(event_type.value, pattern) for pattern in patterns)


def reaches_batch(event: DomainEvent) -> bool:
    """Whether ``event`` can reach a batch ``status_webhook`` (a ``batch.*`` event with ``batch_id``)."""
    return event.batch_id is not None and event.type.value.startswith(_BATCH_PREFIX)


def reaches_conversation(event: DomainEvent) -> bool:
    """Whether ``event`` can reach a conversation ``webhook`` (``sms.*``/``conversation.*``)."""
    return event.conversation_id is not None and event.type.value.startswith(_CONVERSATION_PREFIXES)


def reaches_widget(event: DomainEvent) -> bool:
    """Whether ``event`` can reach a widget ``webhook_url`` (``widget.message``/``conversation.*``)."""
    if event.widget_id is None:
        return False
    return event.type is EventType.WIDGET_MESSAGE or event.type.value.startswith(_WIDGET_PREFIXES)


def _call_target(event: DomainEvent, facts: RoutingFacts) -> Target | None:
    call = facts.call
    if event.call_id is None or call is None or not call.webhook or not wants(event, call):
        return None
    return Target("call", call.webhook)


def _batch_target(event: DomainEvent, facts: RoutingFacts) -> Target | None:
    if not reaches_batch(event) or facts.batch is None or not facts.batch_url:
        return None
    return Target("batch", facts.batch_url)


def _conversation_target(event: DomainEvent, facts: RoutingFacts) -> Target | None:
    if not reaches_conversation(event) or not facts.conversation_url:
        return None
    return Target("conversation", facts.conversation_url)


def _widget_target(event: DomainEvent, facts: RoutingFacts) -> Target | None:
    if not reaches_widget(event) or not facts.widget_url:
        return None
    return Target("widget", facts.widget_url)


def plan_targets(event: DomainEvent, facts: RoutingFacts) -> list[Target]:
    """Every destination of ``event``, in routing order, one per URL."""
    candidates: list[Target | None] = [
        _call_target(event, facts),
        _batch_target(event, facts),
        _conversation_target(event, facts),
        _widget_target(event, facts),
    ]
    candidates.extend(
        Target("endpoint", endpoint.url, endpoint.id, endpoint.phi_allowed)
        for endpoint in facts.endpoints
        if matches(event.type, endpoint.event_types)
    )
    seen: set[str] = set()
    targets: list[Target] = []
    for target in candidates:
        if target is not None and target.url not in seen:
            seen.add(target.url)
            targets.append(target)
    return targets


def _format_of(target: Target, policy: DeliveryPolicy) -> WebhookFormat:
    if target.kind == "batch":
        return "bland"
    if target.kind == "call":
        return policy.call_format
    return "envelope"


def render_deliveries(
    event: DomainEvent, targets: Sequence[Target], facts: RoutingFacts, policy: DeliveryPolicy
) -> list[PlannedDelivery]:
    """The rows for ``targets``: bodies in each target's format, transcripts stripped under HIPAA unless the
    target is an endpoint with ``phi_allowed``."""
    metadata = facts.call.metadata if facts.call is not None else None
    content = content_body(event, metadata=metadata, batch=facts.batch)
    stripped: Body | None = None
    deliveries: list[PlannedDelivery] = []
    for target in targets:
        body = content
        if policy.hipaa and not target.phi_allowed:
            if stripped is None:
                stripped = strip_phi(event.type, content)
            body = stripped
        fmt = _format_of(target, policy)
        payload = envelope_body(event, body) if fmt == "envelope" else body
        deliveries.append(PlannedDelivery(target.url, fmt, payload, target.endpoint_id))
    return deliveries


def plan_deliveries(
    event: DomainEvent, facts: RoutingFacts, policy: DeliveryPolicy | None = None
) -> list[PlannedDelivery]:
    """:func:`plan_targets` then :func:`render_deliveries`."""
    return render_deliveries(event, plan_targets(event, facts), facts, policy or DeliveryPolicy())
