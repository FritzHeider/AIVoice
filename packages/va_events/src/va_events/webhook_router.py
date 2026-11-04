"""The database webhook router (specs/19-events-and-webhooks.md §1 step 5, §3): the transactional outbox.

:class:`DbWebhookRouter` implements :class:`va_events.publisher.WebhookRouter`. With the caller's session it
inserts the ``webhook_deliveries`` rows in the caller's transaction, so a rolled-back state change leaves no
delivery behind; without one it opens its own ``tenant_session(event.org_id)``. Lookups are lazy: the call,
batch, conversation, and widget are read only for events that can reach them, and the org's settings only when
at least one delivery is planned (from ``orgs.settings`` in the same session; ``OrgSettingsReader`` would
open a second pool session). Inserts are ``ON CONFLICT (event_id, url) DO NOTHING``, so routing an event
twice is harmless.

The media worker routes every buffered call event of a flush through here, so the per-event work stays small
(specs/07 §9 Decision P08-FIX-3): a batch reads each call's facts and each org's endpoints once, and the hot
lookups are statements built once at import with bound parameters (building a statement per event made
cyclic garbage that lengthened the collector's pauses on the media loop).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

from pydantic import JsonValue
from sqlalchemy import Table, bindparam, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.ids import new_id
from va_db.models import Batch, Call, Conversation, Org, WebhookDelivery, WebhookEndpoint, Widget
from va_db.session import Database
from va_domain.org import load_settings
from va_events.model import DomainEvent
from va_events.webhook_payloads import BatchFacts
from va_events.webhook_routing import (
    CallFacts,
    DeliveryPolicy,
    EndpointTarget,
    PlannedDelivery,
    RoutingFacts,
    plan_targets,
    reaches_batch,
    reaches_conversation,
    reaches_widget,
    render_deliveries,
)

__all__ = ["DbWebhookRouter", "batch_facts", "call_metadata"]


class DbWebhookRouter:
    """Routes domain events into ``webhook_deliveries`` rows."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def route(self, event: DomainEvent, *, session: AsyncSession | None = None) -> None:
        """Insert the delivery rows of ``event`` (in ``session``'s transaction when given)."""
        await self.route_many([event], session=session)

    async def route_many(self, events: Sequence[DomainEvent], *, session: AsyncSession | None = None) -> None:
        """Insert the delivery rows of every event; without a session, one transaction per org."""
        if session is not None:
            await _route_all(session, events)
            return
        by_org: defaultdict[uuid.UUID, list[DomainEvent]] = defaultdict(list)
        for event in events:
            by_org[event.org_id].append(event)
        for org_id, org_events in by_org.items():
            async with self._database.tenant_session(org_id) as own:
                await _route_all(own, org_events)


async def call_metadata(
    session: AsyncSession, org_id: uuid.UUID, call_id: uuid.UUID
) -> Mapping[str, JsonValue] | None:
    """The ``metadata`` of the org's call; ``None`` when there is no such call."""
    facts = await _call(session, org_id, call_id)
    return None if facts is None else facts.metadata


async def batch_facts(session: AsyncSession, org_id: uuid.UUID, batch_id: uuid.UUID) -> BatchFacts | None:
    """The webhook facts of the org's batch; ``None`` when there is no such batch."""
    found = await _batch(session, org_id, batch_id)
    return None if found is None else found[0]


_CALL_FACTS = select(
    Call.config["webhook"].astext,
    Call.config["webhook_events"],
    Call.metadata_,
).where(Call.org_id == bindparam("org_id"), Call.id == bindparam("call_id"))

_ENDPOINTS = (
    select(WebhookEndpoint.id, WebhookEndpoint.url, WebhookEndpoint.event_types, WebhookEndpoint.phi_allowed)
    .where(WebhookEndpoint.org_id == bindparam("org_id"), WebhookEndpoint.enabled.is_(True))
    .order_by(WebhookEndpoint.created_at, WebhookEndpoint.id)
)

_ORG_SETTINGS = select(Org.settings).where(Org.id == bindparam("org_id"))

_INSERT_DELIVERIES = insert(cast("Table", WebhookDelivery.__table__)).on_conflict_do_nothing(
    index_elements=["event_id", "url"]
)


class _BatchLookups:
    """Call facts and endpoints read once per batch (they do not change inside the caller's transaction)."""

    __slots__ = ("calls", "endpoints", "policies", "session")

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.calls: dict[tuple[uuid.UUID, uuid.UUID], CallFacts | None] = {}
        self.endpoints: dict[uuid.UUID, tuple[EndpointTarget, ...]] = {}
        self.policies: dict[uuid.UUID, DeliveryPolicy] = {}

    async def call(self, org_id: uuid.UUID, call_id: uuid.UUID) -> CallFacts | None:
        key = (org_id, call_id)
        if key not in self.calls:
            self.calls[key] = await _call(self.session, org_id, call_id)
        return self.calls[key]

    async def org_endpoints(self, org_id: uuid.UUID) -> tuple[EndpointTarget, ...]:
        found = self.endpoints.get(org_id)
        if found is None:
            found = self.endpoints[org_id] = await _endpoints(self.session, org_id)
        return found

    async def policy(self, org_id: uuid.UUID) -> DeliveryPolicy:
        found = self.policies.get(org_id)
        if found is None:
            found = self.policies[org_id] = await _policy(self.session, org_id)
        return found


async def _route_all(session: AsyncSession, events: Iterable[DomainEvent]) -> None:
    lookups = _BatchLookups(session)
    for event in events:
        facts = await _facts(lookups, event)
        targets = plan_targets(event, facts)
        if not targets:
            continue
        policy = await lookups.policy(event.org_id)
        await _insert(session, event, render_deliveries(event, targets, facts, policy))


async def _facts(lookups: _BatchLookups, event: DomainEvent) -> RoutingFacts:
    session, org_id = lookups.session, event.org_id
    batch = await _batch(session, org_id, event.batch_id) if event.batch_id and reaches_batch(event) else None
    return RoutingFacts(
        call=await lookups.call(org_id, event.call_id) if event.call_id is not None else None,
        batch=batch[0] if batch is not None else None,
        batch_url=batch[1] if batch is not None else None,
        conversation_url=(
            await _conversation_url(session, org_id, event.conversation_id)
            if event.conversation_id and reaches_conversation(event)
            else None
        ),
        widget_url=(
            await _widget_url(session, org_id, event.widget_id)
            if event.widget_id and reaches_widget(event)
            else None
        ),
        endpoints=await lookups.org_endpoints(org_id),
    )


def _json_object(value: Any) -> Mapping[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _webhook_events(value: Any) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, str))


async def _call(session: AsyncSession, org_id: uuid.UUID, call_id: uuid.UUID) -> CallFacts | None:
    row = (await session.execute(_CALL_FACTS, {"org_id": org_id, "call_id": call_id})).first()
    if row is None:
        return None
    webhook, webhook_events, metadata = row
    return CallFacts(
        webhook=webhook or None,
        webhook_events=_webhook_events(webhook_events),
        metadata=_json_object(metadata),
    )


async def _batch(
    session: AsyncSession, org_id: uuid.UUID, batch_id: uuid.UUID
) -> tuple[BatchFacts, str | None] | None:
    batch = await session.scalar(select(Batch).where(Batch.org_id == org_id, Batch.id == batch_id))
    if batch is None:
        return None
    facts = BatchFacts(
        batch_id=batch.id,
        calls_total=batch.calls_total,
        calls_dispatched=batch.calls_dispatched,
        calls_completed=batch.calls_completed,
        calls_successful=batch.calls_successful,
        calls_failed=batch.calls_failed,
        created_at=batch.created_at,
        updated_at=batch.updated_at,
        completed_at=batch.completed_at,
        failure_code=batch.failure_code,
        failure_reason=batch.failure_reason,
    )
    return facts, batch.status_webhook


async def _conversation_url(
    session: AsyncSession, org_id: uuid.UUID, conversation_id: uuid.UUID
) -> str | None:
    return await session.scalar(
        select(Conversation.webhook).where(Conversation.org_id == org_id, Conversation.id == conversation_id)
    )


async def _widget_url(session: AsyncSession, org_id: uuid.UUID, widget_id: uuid.UUID) -> str | None:
    return await session.scalar(
        select(Widget.webhook_url).where(
            Widget.org_id == org_id, Widget.id == widget_id, Widget.deleted_at.is_(None)
        )
    )


async def _endpoints(session: AsyncSession, org_id: uuid.UUID) -> tuple[EndpointTarget, ...]:
    rows = await session.execute(_ENDPOINTS, {"org_id": org_id})
    return tuple(
        EndpointTarget(id=endpoint_id, url=url, event_types=tuple(event_types), phi_allowed=phi_allowed)
        for endpoint_id, url, event_types, phi_allowed in rows
    )


async def _policy(session: AsyncSession, org_id: uuid.UUID) -> DeliveryPolicy:
    stored = await session.scalar(_ORG_SETTINGS, {"org_id": org_id})
    settings = load_settings(stored or {})
    return DeliveryPolicy(hipaa=settings.hipaa_mode, call_format=settings.webhook_format)


async def _insert(session: AsyncSession, event: DomainEvent, planned: Sequence[PlannedDelivery]) -> None:
    rows = [
        {
            "id": new_id(),
            "org_id": event.org_id,
            "event_id": event.id,
            "event_type": event.type.value,
            "url": delivery.url,
            "endpoint_id": delivery.endpoint_id,
            "payload": delivery.payload,
            "format": delivery.format,
            "status": "pending",
            "call_id": event.call_id,
            "batch_id": event.batch_id,
            "conversation_id": event.conversation_id,
        }
        for delivery in planned
    ]
    if rows:
        await session.execute(_INSERT_DELIVERIES, rows)
