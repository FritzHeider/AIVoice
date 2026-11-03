"""An org's webhook endpoints and delivery history (specs/19-events-and-webhooks.md §10; specs/03 §9).

Rules: specs/19 §10 Decision P06-T04b. Both repositories run on a ``tenant_session`` (RLS plus the explicit
``org_id`` filter of :class:`TenantRepository`). Deleting an endpoint cancels its ``pending``/``delivering``
rows (``dead`` with ``last_error = 'endpoint_deleted'``) in the same transaction; the foreign key then sets
``endpoint_id`` NULL on the rest. A replay restarts the attempt series of the same row (same ``event_id``,
URL, and payload).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import ColumnElement, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.errors import AppError
from va_common.time import Clock
from va_db.models import WebhookDelivery, WebhookEndpoint
from va_db.models.billing import WebhookDeliveryStatus
from va_db.pagination import DEFAULT_PAGE_LIMIT, Page
from va_db.repositories.base import TenantRepository

__all__ = [
    "ALREADY_QUEUED_MESSAGE",
    "ENDPOINT_DELETED_ERROR",
    "ENDPOINT_DELETED_MESSAGE",
    "ENDPOINT_DISABLED_MESSAGE",
    "WebhookDeliveryFilter",
    "WebhookDeliveryRepo",
    "WebhookEndpointRepo",
]

ENDPOINT_DELETED_ERROR: Final = "endpoint_deleted"
"""``webhook_deliveries.last_error`` of a delivery cancelled because its endpoint was deleted."""
ALREADY_QUEUED_MESSAGE: Final = "The delivery is already queued."
ENDPOINT_DELETED_MESSAGE: Final = "The delivery's endpoint was deleted."
ENDPOINT_DISABLED_MESSAGE: Final = "Enable the webhook endpoint before replaying its deliveries."

_QUEUED: Final = ("pending", "delivering")
_REPLAYABLE: Final = ("succeeded", "failed", "dead")


class WebhookEndpointRepo(TenantRepository[WebhookEndpoint]):
    """One org's ``webhook_endpoints`` rows."""

    def __init__(self, session: AsyncSession, org_id: uuid.UUID, *, clock: Clock | None = None) -> None:
        super().__init__(WebhookEndpoint, session, org_id, clock=clock)

    async def all(self) -> list[WebhookEndpoint]:
        """Every endpoint of the org, oldest first."""
        statement = (
            select(WebhookEndpoint)
            .where(WebhookEndpoint.org_id == self.org_id)
            .order_by(WebhookEndpoint.created_at, WebhookEndpoint.id)
        )
        return list((await self.session.scalars(statement)).all())

    async def count(self) -> int:
        """How many endpoints the org has."""
        statement = select(func.count()).where(WebhookEndpoint.org_id == self.org_id)
        return int(await self.session.scalar(statement) or 0)

    async def delete(self, endpoint_id: uuid.UUID) -> int:
        """Delete the endpoint and cancel its queued deliveries; returns how many were cancelled."""
        endpoint = await self.get(endpoint_id)
        cancel = (
            update(WebhookDelivery)
            .where(
                WebhookDelivery.org_id == self.org_id,
                WebhookDelivery.endpoint_id == endpoint.id,
                WebhookDelivery.status.in_(_QUEUED),
            )
            .values(status="dead", last_error=ENDPOINT_DELETED_ERROR)
            .returning(WebhookDelivery.id)
            .execution_options(synchronize_session=False)
        )
        cancelled = len((await self.session.scalars(cancel)).all())
        await self.session.execute(
            delete(WebhookEndpoint)
            .where(WebhookEndpoint.org_id == self.org_id, WebhookEndpoint.id == endpoint.id)
            .execution_options(synchronize_session=False)
        )
        self.session.expunge(endpoint)
        return cancelled


@dataclass(frozen=True, slots=True)
class WebhookDeliveryFilter:
    """Optional exact-match filters of the deliveries list."""

    status: WebhookDeliveryStatus | None = None
    event_type: str | None = None
    call_id: uuid.UUID | None = None
    endpoint_id: uuid.UUID | None = None

    def conditions(self) -> list[ColumnElement[bool]]:
        """The SQL conditions of the filters that are set."""
        conditions: list[ColumnElement[bool]] = []
        if self.status is not None:
            conditions.append(WebhookDelivery.status == self.status)
        if self.event_type is not None:
            conditions.append(WebhookDelivery.event_type == self.event_type)
        if self.call_id is not None:
            conditions.append(WebhookDelivery.call_id == self.call_id)
        if self.endpoint_id is not None:
            conditions.append(WebhookDelivery.endpoint_id == self.endpoint_id)
        return conditions


class WebhookDeliveryRepo(TenantRepository[WebhookDelivery]):
    """One org's ``webhook_deliveries`` rows (the outbox seen from the API)."""

    def __init__(self, session: AsyncSession, org_id: uuid.UUID, *, clock: Clock | None = None) -> None:
        super().__init__(WebhookDelivery, session, org_id, clock=clock)

    async def page(
        self, filters: WebhookDeliveryFilter, *, limit: int = DEFAULT_PAGE_LIMIT, cursor: str | None = None
    ) -> Page[WebhookDelivery]:
        """One page of matching rows, newest first, after ``cursor`` when given."""
        return await self.list(limit=limit, cursor=cursor, where=filters.conditions())

    async def replay(self, delivery_id: uuid.UUID, *, now: datetime) -> WebhookDelivery:
        """Queue a finished delivery again from attempt 1; ``NOT_FOUND`` or ``CONFLICT`` when it cannot be."""
        current = await self.get(delivery_id)
        if current.status in _QUEUED:
            raise AppError("CONFLICT", ALREADY_QUEUED_MESSAGE)
        if current.last_error == ENDPOINT_DELETED_ERROR:
            raise AppError("CONFLICT", ENDPOINT_DELETED_MESSAGE)
        if current.endpoint_id is not None:
            enabled = await self.session.scalar(
                select(WebhookEndpoint.enabled).where(
                    WebhookEndpoint.org_id == self.org_id, WebhookEndpoint.id == current.endpoint_id
                )
            )
            if enabled is False:
                raise AppError("CONFLICT", ENDPOINT_DISABLED_MESSAGE)
        statement = (
            update(WebhookDelivery)
            .where(
                WebhookDelivery.org_id == self.org_id,
                WebhookDelivery.id == delivery_id,
                WebhookDelivery.status.in_(_REPLAYABLE),
            )
            .values(
                status="pending",
                attempts=0,
                next_attempt_at=now,
                last_status_code=None,
                last_error=None,
                last_response_ms=None,
                delivered_at=None,
            )
            .returning(WebhookDelivery)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        replayed = await self.session.scalar(statement)
        if replayed is None:
            raise AppError("CONFLICT", ALREADY_QUEUED_MESSAGE)
        return replayed
