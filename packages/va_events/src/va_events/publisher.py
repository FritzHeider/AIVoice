"""The event publisher (specs/19-events-and-webhooks.md §1 steps 1-5).

``publish`` assigns the per-call ``seq`` (``MULTI INCR + EXPIRE``, TTL refreshed on every event), then
sends ``XADD events:<stream> MAXLEN ~ 1000000 * payload <json>`` and, for call events,
``PUBLISH call:{<id>}:live`` in one non-transactional pipeline. A Redis failure in these steps raises
:class:`EventPublishError` and nothing is persisted or routed (a ``call_events`` row needs its seq). Then the
``call_events`` row goes to the sink and the event to the :class:`WebhookRouter` (phase 06), with the
caller's session for the transactional outbox.

Seqs are gap-free and strictly increasing per call; XADD order across concurrent publishers may differ from
seq order, so consumers order call events by ``seq``.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from va_events.call_events import CallEventSink, row_for
from va_events.catalog import stream_for
from va_events.keys import CALL_SEQ_TTL_S, STREAM_MAXLEN, call_live_channel, call_seq_key, stream_key
from va_events.model import DomainEvent, encode

__all__ = ["EventPublishError", "EventPublisher", "WebhookRouter"]

PAYLOAD_FIELD = "payload"


class WebhookRouter(Protocol):
    """Turns an event into ``webhook_deliveries`` rows (implemented by the webhook phase)."""

    async def route(self, event: DomainEvent, *, session: AsyncSession | None) -> None: ...


class EventPublishError(Exception):
    """Redis refused the seq, stream, or live step; the event was not persisted or routed."""

    def __init__(self, event: DomainEvent, step: str) -> None:
        super().__init__(f"publishing {event.type.value} ({event.id}) failed at {step}")
        self.event = event
        self.step = step


class EventPublisher:
    """Publishes domain events to the Redis bus, the ``call_events`` sink, and the webhook router."""

    def __init__(
        self,
        redis: Redis,
        *,
        call_events: CallEventSink | None,
        router: WebhookRouter | None = None,
    ) -> None:
        self._redis = redis
        self._call_events = call_events
        self._router = router

    async def publish(
        self,
        event: DomainEvent,
        *,
        persist_call_event: bool = True,
        deliver_webhooks: bool = True,
        session: AsyncSession | None = None,
    ) -> DomainEvent:
        """Publish ``event``; returns it with ``seq`` set for call events."""
        if event.call_id is not None:
            event = event.model_copy(update={"seq": await self._next_seq(event, event.call_id)})
        await self._broadcast(event)
        if persist_call_event and self._call_events is not None:
            row = row_for(event)
            if row is not None:
                await self._call_events.add(row)
        if deliver_webhooks and self._router is not None:
            await self._router.route(event, session=session)
        return event

    async def _next_seq(self, event: DomainEvent, call_id: uuid.UUID) -> int:
        key = call_seq_key(call_id)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, CALL_SEQ_TTL_S)
                seq, _ = await pipe.execute()
        except (RedisError, OSError, TimeoutError) as exc:
            raise EventPublishError(event, "seq") from exc
        return int(seq)

    async def _broadcast(self, event: DomainEvent) -> None:
        payload = encode(event)
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.xadd(
                    stream_key(stream_for(event.type)),
                    {PAYLOAD_FIELD: payload},
                    maxlen=STREAM_MAXLEN,
                    approximate=True,
                )
                if event.call_id is not None:
                    pipe.publish(call_live_channel(event.call_id), payload)
                await pipe.execute()
        except (RedisError, OSError, TimeoutError) as exc:
            raise EventPublishError(event, "stream") from exc
