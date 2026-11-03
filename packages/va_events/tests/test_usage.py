"""``billing.usage``: the internal usage mirror event (specs/19 §2; §1 Decision P06-T05)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from event_helpers import ORG, publisher_rig, stream_events
from pydantic import ValidationError

from va_common.time import FrozenClock
from va_events import INTERNAL_TYPES, EventType, UsageEventData, stream_for, usage_event

NOW = datetime(2026, 11, 3, 9, tzinfo=UTC)


def usage(**over: object) -> UsageEventData:
    values: dict[str, object] = {
        "meter": "call_connected_seconds",
        "quantity": Decimal("61.500000"),
        "amount_micros": 92_250,
        "reference_type": "call",
        "reference_id": str(uuid.uuid4()),
        "occurred_at": NOW,
        "idempotency_key": "call:x:connected",
        **over,
    }
    return UsageEventData.model_validate(values)


def test_the_event_carries_the_usage_row_as_json() -> None:
    data = usage(occurred_at=datetime(2026, 11, 3, 10, tzinfo=timezone(timedelta(hours=1))))
    event = usage_event(data, org_id=ORG, clock=FrozenClock(NOW))
    assert (event.type, event.org_id, event.call_id, event.occurred_at) == (
        EventType.BILLING_USAGE,
        ORG,
        None,
        NOW,
    )
    assert event.data["quantity"] == "61.500000"
    assert event.data["cost_micros"] == 0
    assert event.data["occurred_at"] == "2026-11-03T09:00:00Z"
    assert UsageEventData.model_validate(event.data) == data
    assert stream_for(event.type) == "billing"
    assert EventType.BILLING_USAGE in INTERNAL_TYPES


@pytest.mark.parametrize("over", [{"meter": "minutes"}, {"surprise": 1}, {"amount_micros": "lots"}])
def test_invalid_usage_data_is_rejected(over: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        usage(**over)


async def test_it_is_published_to_the_billing_stream_without_a_seq() -> None:
    rig = publisher_rig()
    published = await rig.publisher.publish(usage_event(usage(), org_id=ORG, clock=FrozenClock(NOW)))
    assert published.seq is None
    assert await stream_events(rig.redis, "events:billing") == [published]
    assert rig.writer.rows == []
