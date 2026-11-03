"""The webhook outbox poll and delivery bookkeeping (specs/03-data-model.md §2; specs/19 §8).

The retry schedule and the dead-letter decision belong to the dispatcher; this repository only claims due
rows and records each attempt's outcome. An outcome is recorded only on a row that is still ``delivering``:
a row cancelled meanwhile (endpoint deleted, specs/19 §10 Decision P06-T04b) raises
:class:`DeliveryCancelledError` and keeps its state.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, select, update

from va_db.models import WebhookDelivery, WebhookEndpoint
from va_db.repositories.base import SystemRepository, require_positive

__all__ = ["CLAIM_LIMIT", "DEFAULT_LEASE", "DeliveryCancelledError", "OutboxBacklog", "OutboxRepo"]

CLAIM_LIMIT: Final = 200
DEFAULT_LEASE: Final = timedelta(minutes=5)

_DUE_STATUSES: Final = ("pending", "delivering")


class DeliveryCancelledError(Exception):
    """The attempt's row is no longer ``delivering`` (cancelled by an endpoint delete, or gone)."""

    def __init__(self, delivery_id: uuid.UUID) -> None:
        super().__init__(f"webhook delivery {delivery_id} is no longer delivering")
        self.delivery_id = delivery_id


@dataclass(frozen=True, slots=True)
class OutboxBacklog:
    """Due deliveries (``pending``/``delivering`` with ``next_attempt_at <= now``) and the oldest due time."""

    due: int
    oldest_due_at: datetime | None


class OutboxRepo(SystemRepository):
    """Claims due ``webhook_deliveries`` across orgs and records attempts (system scope, specs/03 §2)."""

    async def claim_due(
        self, now: datetime, *, limit: int = CLAIM_LIMIT, lease: timedelta = DEFAULT_LEASE
    ) -> list[WebhookDelivery]:
        """Mark up to ``limit`` due rows ``delivering`` and return them (order not guaranteed).

        Due means ``pending`` or ``delivering`` with ``next_attempt_at <= now``, oldest first, skipping rows
        another dispatcher has locked. ``next_attempt_at`` moves to ``now + lease``, so the rows of a
        dispatcher that crashes mid-delivery become due again once the lease runs out.
        """
        due = (
            select(WebhookDelivery.id)
            .where(WebhookDelivery.status.in_(_DUE_STATUSES), WebhookDelivery.next_attempt_at <= now)
            .order_by(WebhookDelivery.next_attempt_at, WebhookDelivery.id)
            .limit(require_positive("limit", limit))
            .with_for_update(skip_locked=True)
        )
        statement = (
            update(WebhookDelivery)
            .where(WebhookDelivery.id.in_(due))
            .values(status="delivering", next_attempt_at=now + lease)
            .returning(WebhookDelivery)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        return list((await self.session.scalars(statement)).all())

    async def backlog(self, now: datetime) -> OutboxBacklog:
        """How many rows are due at ``now`` and the earliest ``next_attempt_at`` among them."""
        statement = select(func.count(), func.min(WebhookDelivery.next_attempt_at)).where(
            WebhookDelivery.status.in_(_DUE_STATUSES), WebhookDelivery.next_attempt_at <= now
        )
        due, oldest = (await self.session.execute(statement)).one()
        return OutboxBacklog(due=int(due), oldest_due_at=oldest)

    async def endpoint_enabled(self, endpoint_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, bool]:
        """``enabled`` of each existing endpoint among ``endpoint_ids`` (deleted endpoints are absent)."""
        ids = set(endpoint_ids)
        if not ids:
            return {}
        statement = select(WebhookEndpoint.id, WebhookEndpoint.enabled).where(WebhookEndpoint.id.in_(ids))
        return {row.id: row.enabled for row in (await self.session.execute(statement)).all()}

    async def disable_endpoint(self, endpoint_id: uuid.UUID) -> bool:
        """Disable an enabled endpoint; ``True`` only for the call that flipped it (410 auto-disable)."""
        statement = (
            update(WebhookEndpoint)
            .where(WebhookEndpoint.id == endpoint_id, WebhookEndpoint.enabled)
            .values(enabled=False)
            .returning(WebhookEndpoint.id)
            .execution_options(synchronize_session=False)
        )
        return (await self.session.scalar(statement)) is not None

    async def _finish(self, delivery_id: uuid.UUID, values: dict[str, Any]) -> WebhookDelivery:
        statement = (
            update(WebhookDelivery)
            .where(WebhookDelivery.id == delivery_id, WebhookDelivery.status == "delivering")
            .values(attempts=WebhookDelivery.attempts + 1, **values)
            .returning(WebhookDelivery)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        delivery = await self.session.scalar(statement)
        if delivery is None:
            raise DeliveryCancelledError(delivery_id)
        return delivery

    async def mark_succeeded(
        self, delivery_id: uuid.UUID, *, now: datetime, status_code: int, response_ms: int
    ) -> WebhookDelivery:
        """Record a successful attempt: ``succeeded``, ``delivered_at = now``, no ``last_error``."""
        return await self._finish(
            delivery_id,
            {
                "status": "succeeded",
                "delivered_at": now,
                "last_status_code": status_code,
                "last_response_ms": response_ms,
                "last_error": None,
            },
        )

    async def schedule_retry(
        self,
        delivery_id: uuid.UUID,
        *,
        next_attempt_at: datetime,
        status_code: int | None,
        error: str,
        response_ms: int | None,
    ) -> WebhookDelivery:
        """Record a failed attempt and make the row ``pending`` again at ``next_attempt_at``."""
        return await self._finish(
            delivery_id,
            {
                "status": "pending",
                "next_attempt_at": next_attempt_at,
                "last_status_code": status_code,
                "last_error": error,
                "last_response_ms": response_ms,
            },
        )

    async def mark_dead(
        self, delivery_id: uuid.UUID, *, status_code: int | None, error: str, response_ms: int | None
    ) -> WebhookDelivery:
        """Record a final failed attempt: ``dead`` (never claimed again)."""
        return await self._finish(
            delivery_id,
            {
                "status": "dead",
                "last_status_code": status_code,
                "last_error": error,
                "last_response_ms": response_ms,
            },
        )
