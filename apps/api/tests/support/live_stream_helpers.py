"""Shared pieces of the call live stream tests (SSE and transcript WebSocket;
specs/19-events-and-webhooks.md §9): a manual clock inside the ``call_events`` partitions, a seeded call with
a key, event publishing into ``call_events`` and/or the live channel, a watch for the hub releasing a
channel, and a reader of a stream's raw SSE frames."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Final

from call_rows import seed_call
from clock_helpers import ManualClock
from session_helpers import SessionContainer, store_api_key

from va_common.time import FrozenClock
from va_events import DomainEvent, EventType, LiveHub, insert_rows, new_event, row_for

__all__ = [
    "FrameReader",
    "ReleaseWatch",
    "next_frame",
    "partition_clock",
    "publish_event",
    "started",
    "streamed_call",
]

READ_WAIT_S: Final = 5.0


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


class FrameReader:
    """Reads one stream's raw frames (``frames`` is the stream's ``frames()`` generator); :func:`started`
    checks and skips the ``retry`` preamble."""

    def __init__(self, frames: AsyncGenerator[bytes]) -> None:
        self.frames = frames
        self.pending: asyncio.Future[bytes] | None = None

    async def start(self) -> None:
        assert await self.raw() == b"retry: 3000\n\n"

    def ask(self) -> asyncio.Future[bytes]:
        """Start reading the next frame (idempotent until it is taken with :meth:`raw`)."""
        if self.pending is None:
            self.pending = asyncio.ensure_future(anext(self.frames))
        return self.pending

    async def raw(self) -> bytes:
        future = self.ask()
        async with asyncio.timeout(READ_WAIT_S):
            value = await future
        self.pending = None
        return value

    async def ended(self) -> bool:
        try:
            await self.raw()
        except StopAsyncIteration:
            return True
        return False

    async def aclose(self) -> None:
        if self.pending is not None:
            self.pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await self.pending
        await self.frames.aclose()


@asynccontextmanager
async def started[R: FrameReader](reader: R) -> AsyncIterator[R]:
    """``reader`` past the ``retry`` preamble, closed on exit."""
    await reader.start()
    try:
        yield reader
    finally:
        await reader.aclose()


async def next_frame(chunks: AsyncIterator[bytes], buffer: bytearray) -> bytes:
    """The next whole SSE frame of an HTTP body (``chunks`` = ``response.aiter_bytes()``; ``buffer`` keeps the
    bytes read past it)."""
    async with asyncio.timeout(READ_WAIT_S):
        while b"\n\n" not in buffer:
            buffer.extend(await anext(chunks))
    end = buffer.index(b"\n\n") + 2
    frame = bytes(buffer[:end])
    del buffer[:end]
    return frame
