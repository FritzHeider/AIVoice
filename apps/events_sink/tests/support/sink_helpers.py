"""Events sink test helpers: scripted ClickHouse connections, a clock that records its sleeps, and builders of
the events the sink maps."""

from __future__ import annotations

import asyncio
import copy
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from ch_support import FakeClickHouse
from clock_helpers import RecordingClock
from pydantic import JsonValue

from va_common.time import FrozenClock
from va_domain.call_object import CallObject
from va_events import DomainEvent, EventType, UsageEventData, new_event, usage_event

__all__ = [
    "AT",
    "ORG",
    "FakeConnect",
    "RecordingClock",
    "call_object_data",
    "completed_event",
    "never_connects",
    "turn_event",
    "usage_data",
    "usage_event_of",
]

ORG: Final = uuid.UUID("0193c5d2-0000-7000-8000-0000000000a1")
AT: Final = datetime(2026, 11, 3, 9, 30, tzinfo=UTC)
_CLOCK: Final = FrozenClock(AT)


def turn_event(  # noqa: PLR0913 - one call.turn event
    call_id: uuid.UUID,
    seq: int,
    *,
    role: str = "assistant",
    text: str = "Hello there",
    latency: dict[str, JsonValue] | None = None,
    org_id: uuid.UUID = ORG,
    **data: JsonValue,
) -> DomainEvent:
    """A published ``call.turn`` (``seq`` set) as the media worker emits it."""
    body: dict[str, JsonValue] = {
        "turn_index": seq,
        "role": role,
        "text": text,
        "node_id": None,
        "interrupted": False,
        **data,
    }
    if latency is not None:
        body["latency"] = latency
    event = new_event(
        EventType.CALL_TURN, org_id=org_id, clock=_CLOCK, call_id=call_id, message=text, data=body
    )
    return event.model_copy(update={"seq": seq})


def call_object_data(call_id: uuid.UUID, *, org_id: uuid.UUID = ORG, **over: Any) -> dict[str, JsonValue]:
    """The Call Object example of specs/19 §4 for ``call_id`` (dumped like the post-call activity does)."""
    extra: Any = CallObject.model_config.get("json_schema_extra")
    example = copy.deepcopy(extra["examples"][0])
    example.update(call_id=str(call_id), c_id=str(call_id), org_id=str(org_id), **over)
    data: dict[str, JsonValue] = CallObject.model_validate(example).model_dump(mode="json", by_alias=True)
    return data


def completed_event(call_id: uuid.UUID, seq: int, *, org_id: uuid.UUID = ORG, **over: Any) -> DomainEvent:
    """A published ``call.completed`` carrying the Call Object."""
    event = new_event(
        EventType.CALL_COMPLETED,
        org_id=org_id,
        clock=_CLOCK,
        call_id=call_id,
        message="Call completed",
        data=call_object_data(call_id, org_id=org_id, **over),
    )
    return event.model_copy(update={"seq": seq})


def usage_data(**over: Any) -> UsageEventData:
    values: dict[str, Any] = {
        "meter": "call_connected_seconds",
        "quantity": Decimal("61.5"),
        "amount_micros": 92_250,
        "cost_micros": 4_100,
        "reference_type": "call",
        "reference_id": "0193c5d2-7b1e-7a10-9a52-1f3b0d4e6a77",
        "occurred_at": AT,
        "idempotency_key": "call:0193c5d2:connected",
        **over,
    }
    return UsageEventData.model_validate(values)


def usage_event_of(*, org_id: uuid.UUID = ORG, **over: Any) -> DomainEvent:
    return usage_event(usage_data(**over), org_id=org_id, clock=_CLOCK)


@dataclass
class FakeConnect:
    """``SchemaConnect`` that raises the queued ``failures`` one per call, then yields ``client``."""

    client: FakeClickHouse = field(default_factory=FakeClickHouse)
    failures: list[Exception] = field(default_factory=list)
    opened: int = 0
    closed: int = 0

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[FakeClickHouse]:
        self.opened += 1
        if self.failures:
            raise self.failures.pop(0)
        try:
            yield self.client
        finally:
            self.closed += 1


@asynccontextmanager
async def never_connects() -> AsyncIterator[FakeClickHouse]:
    """A connection attempt that hangs until the lifespan cancels it."""
    await asyncio.Event().wait()
    yield FakeClickHouse()
