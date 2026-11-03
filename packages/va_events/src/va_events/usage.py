"""``billing.usage``: the event mirror of one posted ``usage_events`` row (specs/19 §2; specs/24 §4 step 4).

The event goes to ``events:billing`` and is read only by the events sink, which inserts it into the ClickHouse
``va.usage_events_raw`` table (specs/03 §12). It is in :data:`~va_events.catalog.INTERNAL_TYPES`, so the
webhook router never delivers it. ``data`` is :class:`UsageEventData` dumped in JSON mode (``quantity`` as a
decimal string, ``occurred_at`` as ISO 8601).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, JsonValue

from va_common.time import Clock, ensure_utc
from va_db.models import UsageMeter
from va_events.catalog import EventType
from va_events.model import DomainEvent, new_event

__all__ = ["UsageEventData", "usage_event"]


class UsageEventData(BaseModel):
    """The ``usage_events`` columns the analytics mirror carries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    meter: UsageMeter
    quantity: Decimal
    amount_micros: int
    cost_micros: int = 0
    reference_type: str
    reference_id: str
    occurred_at: Annotated[datetime, AfterValidator(ensure_utc)]
    idempotency_key: str


def usage_event(data: UsageEventData, *, org_id: uuid.UUID, clock: Clock) -> DomainEvent:
    """A ``billing.usage`` event of ``org_id`` carrying ``data`` (no call id: no seq, no live message)."""
    payload: dict[str, JsonValue] = data.model_dump(mode="json")
    return new_event(EventType.BILLING_USAGE, org_id=org_id, clock=clock, data=payload)
