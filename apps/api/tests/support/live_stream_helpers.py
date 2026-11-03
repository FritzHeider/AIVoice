"""Shared pieces of the call live stream tests (SSE and transcript WebSocket;
specs/19-events-and-webhooks.md §9): a manual clock inside the ``call_events`` partitions, a seeded call with
a key, event publishing into ``call_events`` and/or the live channel, and a watch for the hub releasing a
channel."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from call_rows import seed_call
from clock_helpers import ManualClock
from session_helpers import SessionContainer, store_api_key

from va_common.time import FrozenClock
from va_events import DomainEvent, EventType, LiveHub, insert_rows, new_event, row_for

__all__ = ["ReleaseWatch", "partition_clock", "publish_event", "streamed_call"]


def partition_clock() -> ManualClock:
    """A manual clock at the real current time: ``call_events`` partitions exist around the current month."""
    manual = ManualClock()
    manual.set(datetime.now(UTC))
    return manual


async def streamed_call(
    container: SessionContainer, *, scopes: tuple[str, ...] = ("*",)
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """An in-progress call of a fresh org (metadata ``{"crm": "7"}``) and an API key of that org."""
    call = await seed_call(container.database, status="in_progress", metadata_={"crm": "7"})
    key, _ = await store_api_key(container, call.org_id, scopes)
    return call.org_id, call.call_id, key


async def publish_event(  # noqa: PLR0913 - the event and where it goes
    container: SessionContainer,
    org_id: uuid.UUID,
    call_id: uuid.UUID,
    event_type: EventType,
    *,
    at: datetime | None = None,
    live: bool = True,
    stored: bool = True,
    **fields: Any,
) -> DomainEvent:
    """Sequence an event of the call, then store its ``call_events`` row and/or broadcast it live."""
    event = new_event(
        event_type, org_id=org_id, clock=FrozenClock(at or container.clock.now()), call_id=call_id, **fields
    )
    event = await container.event_publisher.sequence(event)
    row = row_for(event)
    assert row is not None
    if stored:
        async with container.database.tenant_session(org_id) as session:
            await insert_rows(session, [row])
    if live:
        await container.event_publisher.broadcast(event)
    return event


class ReleaseWatch:
    """Wraps ``hub.unsubscribe``; :attr:`released` is set once the last subscriber of ``channel`` is gone."""

    def __init__(self, hub: LiveHub, channel: str) -> None:
        self.released = asyncio.Event()
        self._hub = hub
        self._channel = channel
        self._unsubscribe = hub.unsubscribe

    async def __call__(self, subscription: object) -> None:
        await self._unsubscribe(subscription)  # type: ignore[arg-type]
        if self._hub.subscribers(self._channel) == 0:
            self.released.set()
