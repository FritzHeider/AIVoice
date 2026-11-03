"""va_events bus test helpers: event builders, a recording ``call_events`` writer and webhook router, and a
publisher on fakeredis."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Unpack

from fakeredis import FakeAsyncRedis, FakeServer
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.time import FrozenClock
from va_events import (
    CallEventBuffer,
    CallEventRow,
    CallEventWriteError,
    DirectCallEventWriter,
    DomainEvent,
    EventFields,
    EventPublisher,
    EventType,
    decode,
    new_event,
)

__all__ = [
    "ORG",
    "PublisherRig",
    "RecordingRouter",
    "RecordingWriter",
    "WriterFailedError",
    "event",
    "publisher_rig",
    "row",
    "stream_events",
    "stream_payloads",
]

ORG = uuid.UUID("0193c5d2-0000-7000-8000-000000000001")
_CLOCK = FrozenClock(datetime(2026, 11, 1, 12, tzinfo=UTC))


def event(event_type: EventType, *, org_id: uuid.UUID = ORG, **fields: Unpack[EventFields]) -> DomainEvent:
    """A fresh event of ``event_type`` at a fixed time."""
    return new_event(event_type, org_id=org_id, clock=_CLOCK, **fields)


def row(call_id: uuid.UUID, seq: int, *, org_id: uuid.UUID = ORG) -> CallEventRow:
    """A ``call_events`` row of ``call_id`` with ``seq``."""
    return CallEventRow(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call_id,
        seq=seq,
        category="call",
        level="info",
        message=f"event {seq}",
        data={"seq": seq},
        created_at=_CLOCK.now(),
    )


class WriterFailedError(RuntimeError):
    """Raised by :class:`RecordingWriter` while armed with ``fail_next``."""

    def __init__(self) -> None:
        super().__init__("database unavailable")


@dataclass
class RecordingWriter:
    """A :class:`va_events.CallEventWriter` that records every batch; ``fail_next(n)`` fails the next n.

    Orgs in ``failing_orgs`` fail like :class:`va_events.DbCallEventWriter` does for one org: the other orgs'
    rows are recorded and :class:`va_events.CallEventWriteError` names the failing orgs' rows.
    """

    batches: list[list[CallEventRow]] = field(default_factory=list)
    attempts: int = 0
    failing_orgs: set[uuid.UUID] = field(default_factory=set)
    _failures: int = 0

    def fail_next(self, count: int = 1) -> None:
        self._failures = count

    async def write(self, rows: Sequence[CallEventRow]) -> None:
        self.attempts += 1
        if self._failures:
            self._failures -= 1
            raise WriterFailedError
        written = [r for r in rows if r.org_id not in self.failing_orgs]
        if written:
            self.batches.append(written)
        unwritten = [r for r in rows if r.org_id in self.failing_orgs]
        if unwritten:
            raise CallEventWriteError(unwritten, failed_orgs=len({r.org_id for r in unwritten}))

    @property
    def rows(self) -> list[CallEventRow]:
        return [r for batch in self.batches for r in batch]


@dataclass
class RecordingRouter:
    """A :class:`va_events.WebhookRouter` that records ``(event, session)``."""

    routed: list[tuple[DomainEvent, AsyncSession | None]] = field(default_factory=list)

    async def route(self, event: DomainEvent, *, session: AsyncSession | None) -> None:
        self.routed.append((event, session))

    async def route_many(self, events: Sequence[DomainEvent], *, session: AsyncSession | None) -> None:
        for event in events:
            await self.route(event, session=session)


@dataclass
class PublisherRig:
    server: FakeServer
    redis: FakeAsyncRedis
    writer: RecordingWriter
    router: RecordingRouter
    publisher: EventPublisher


def publisher_rig(*, router: bool = True, buffer: CallEventBuffer | None = None) -> PublisherRig:
    """A publisher on a fresh fakeredis server; rows go to a :class:`RecordingWriter` (or ``buffer``)."""
    server = FakeServer()
    redis = FakeAsyncRedis(server=server, max_connections=256)
    writer = RecordingWriter()
    recording_router = RecordingRouter()
    publisher = EventPublisher(
        redis,
        call_events=buffer if buffer is not None else DirectCallEventWriter(writer),
        router=recording_router if router else None,
    )
    return PublisherRig(server, redis, writer, recording_router, publisher)


async def stream_payloads(redis: Any, key: str) -> list[bytes]:
    """The ``payload`` field of every entry of stream ``key``, oldest first."""
    entries: list[tuple[bytes, dict[bytes, bytes]]] = await redis.xrange(key)
    return [fields[b"payload"] for _, fields in entries]


async def stream_events(redis: Any, key: str) -> list[DomainEvent]:
    """The decoded events of stream ``key``, oldest first."""
    return [decode(payload) for payload in await stream_payloads(redis, key)]
