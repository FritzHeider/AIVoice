"""Pod-wide tail of the event bus streams for org event streams (specs/19-events-and-webhooks.md §9;
Decision P06-T06c).

Every client of ``GET /v1/events/stream`` wants the new events of one org. A blocking ``XREAD`` per client
would hold one pooled Redis connection each, so a process keeps ONE :class:`StreamTailHub`. The first
:meth:`StreamTailHub.subscribe` reads the last entry id of each of the five streams (``XREVRANGE ... COUNT
1``, ``0-0`` for an empty stream) and starts the reader task; from then on the reader reads every stream from
those ids with one ``XREAD`` (``COUNT 500``, ``BLOCK`` :data:`BLOCK_MS`), so nothing added after the first
subscribe returned is skipped. Each decoded event is copied into the queue of every subscriber of its org
whose predicate accepts it (the predicate runs in the reader, so unwanted events are never queued).
Undecodable entries are skipped and logged ``events.stream_tail_poison``.

There is no replay: events added before a subscription are not delivered. A subscriber whose queue already
holds ``queue_size`` events is closed with ``overflow``; a Redis error closes every subscription with
``lost`` and ends the reader (the next subscribe starts a new one); the last unsubscribe stops the reader.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from typing import Any, Final

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_common.logging import get_logger
from va_events.catalog import STREAMS
from va_events.keys import stream_key
from va_events.model import DomainEvent, decode
from va_events.publisher import PAYLOAD_FIELD
from va_events.subscription import CloseReason, Subscription

__all__ = [
    "BLOCK_MS",
    "QUEUE_SIZE",
    "READ_COUNT",
    "EventPredicate",
    "StreamTailError",
    "StreamTailHub",
    "TailSubscription",
]

BLOCK_MS: Final = 1_000
"""How long one ``XREAD`` blocks; below the 5 s socket timeout of the shared client."""
READ_COUNT: Final = 500
QUEUE_SIZE: Final = 1_000
_START_ID: Final = "0-0"
_REDIS_ERRORS: Final = (RedisError, OSError, TimeoutError)

EventPredicate = Callable[[DomainEvent], bool]

_log = get_logger(__name__)


class StreamTailError(Exception):
    """No subscription is possible: the hub is closed or Redis failed while starting the reader."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"stream tail subscription failed: {reason}")
        self.reason = reason


class TailSubscription(Subscription[DomainEvent]):
    """One subscriber's queue of the new events of :attr:`org_id` that :attr:`predicate` accepts."""

    def __init__(self, org_id: uuid.UUID, predicate: EventPredicate, queue_size: int) -> None:
        super().__init__(queue_size)
        self.org_id = org_id
        self.predicate = predicate


def _text(value: Any) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)


def _payload(fields: Any) -> Any:
    if not isinstance(fields, dict):
        return None
    return fields.get(PAYLOAD_FIELD.encode(), fields.get(PAYLOAD_FIELD))


class StreamTailHub:
    """The process's reader of the event bus streams, shared by every org event stream."""

    def __init__(
        self,
        redis: Redis,
        *,
        block_ms: int = BLOCK_MS,
        queue_size: int = QUEUE_SIZE,
    ) -> None:
        self._redis = redis
        self._block_ms = block_ms
        self._queue_size = queue_size
        self._lock = asyncio.Lock()
        self._reader: asyncio.Task[None] | None = None
        self._members: dict[uuid.UUID, set[TailSubscription]] = {}
        self._closed = False

    @property
    def reader_running(self) -> bool:
        """Whether the reader task is running."""
        return self._reader is not None and not self._reader.done()

    def subscribers(self, org_id: uuid.UUID | None = None) -> int:
        """How many subscribers ``org_id`` has (every org's without it)."""
        if org_id is not None:
            return len(self._members.get(org_id, ()))
        return sum(len(members) for members in self._members.values())

    async def subscribe(self, org_id: uuid.UUID, predicate: EventPredicate) -> TailSubscription:
        """A subscription of the events of ``org_id`` added from now on that ``predicate`` accepts.

        Raises :class:`StreamTailError` after :meth:`aclose` or when the reader cannot start.
        """
        subscription = TailSubscription(org_id, predicate, self._queue_size)
        async with self._lock:
            if self._closed:
                raise StreamTailError("closed")
            if not self.reader_running:
                ids = await self._start_ids()
                self._reader = asyncio.create_task(self._read(ids), name="stream-tail-reader")
            self._members.setdefault(org_id, set()).add(subscription)
        return subscription

    async def _start_ids(self) -> dict[str, str]:
        keys = [stream_key(stream) for stream in STREAMS]
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                for key in keys:
                    pipe.xrevrange(key, "+", "-", count=1)
                results = await pipe.execute()
        except _REDIS_ERRORS as exc:
            _log.warning("events.stream_tail_start_failed", detail=type(exc).__name__)
            raise StreamTailError("redis") from exc
        return {
            key: _text(entries[0][0]) if entries else _START_ID
            for key, entries in zip(keys, results, strict=True)
        }

    async def unsubscribe(self, subscription: TailSubscription) -> None:
        """Remove ``subscription`` (closed with ``closed``); the last one stops the reader."""
        subscription.close("closed")
        async with self._lock:
            members = self._members.get(subscription.org_id)
            if members is None or subscription not in members:
                return
            members.discard(subscription)
            if not members:
                del self._members[subscription.org_id]
            if self._members:
                return
            reader, self._reader = self._reader, None
        await self._stop(reader)

    async def _read(self, ids: dict[str, str]) -> None:
        try:
            while True:
                response: Any = await self._redis.xread(
                    dict(ids),  # type: ignore[arg-type]
                    count=READ_COUNT,
                    block=self._block_ms,
                )
                if not response:
                    # fakeredis (and an idle server after BLOCK) answers at once: let the loop breathe.
                    await asyncio.sleep(0)
                    continue
                for key, entries in response:
                    stream = _text(key)
                    for entry_id, fields in entries:
                        ids[stream] = _text(entry_id)
                        self._dispatch(stream, _text(entry_id), fields)
        except _REDIS_ERRORS as exc:
            _log.warning("events.stream_tail_lost", detail=type(exc).__name__)
            self._end_all("lost")

    def _dispatch(self, stream: str, entry_id: str, fields: Any) -> None:
        payload = _payload(fields)
        try:
            event = decode(payload)
        except (ValidationError, ValueError, TypeError):
            _log.warning("events.stream_tail_poison", stream=stream, entry_id=entry_id)
            return
        for subscription in tuple(self._members.get(event.org_id, ())):
            if subscription.predicate(event):
                subscription.deliver(event)

    def _end_all(self, reason: CloseReason) -> None:
        for members in self._members.values():
            for subscription in members:
                subscription.close(reason)
        self._members.clear()

    @staticmethod
    async def _stop(reader: asyncio.Task[None] | None) -> None:
        if reader is None or reader.done():
            return
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader

    async def aclose(self) -> None:
        """Stop the reader and close every subscription with ``closed``; later subscribes fail."""
        async with self._lock:
            self._closed = True
            reader, self._reader = self._reader, None
            self._end_all("closed")
        await self._stop(reader)
