"""One attached call on this pod (specs/07-media-pipeline.md §1, §17, §19; specs/07 §3 Decisions P05-T07a and
P05-T07b).

A :class:`CallSession` runs the session body (the media transport and pipeline, a seam filled by later tasks)
next to the ``ownership_renewer``, ``heartbeat``, and ``control_listener`` tasks. The first of them to finish
ends the session:

- the body returns a :class:`SessionEnd` (``stream_closed`` when Telnyx stops the stream);
- the renewer finds the ownership lock gone or taken (or no renewal succeeded for the lock TTL) and ends it
  with ``end_reason='worker_lost'`` (no hangup: the workflow issues it);
- :meth:`CallSession.request_end` ends it with the given reason (a control message, the drain timeout).

The control listener hands each message of the call's :class:`~va_media.channels.CallFeed` to the
:class:`~va_media.control.ControlHandler` in arrival order; a handled message from Redis is acknowledged with
``SET call:{<id>}:control_ack:<message id> 1 EX 30`` (the write survives the session ending meanwhile: cleanup
waits for it). A body or handler exception fails the session.

Cleanup always deletes the heartbeat, releases the lock unless it was lost, and closes the WebSocket (1000,
or 1011 when the body raised).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal

from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_common.logging import bound_context, get_logger
from va_common.metrics import ServiceMetrics
from va_common.time import Clock
from va_domain.call_context import CallContext
from va_domain.carrier import heartbeat_key
from va_domain.control import CONTROL_ACK_TTL_S, ControlMessage, control_ack_key
from va_media.carrier_state import CarrierSnapshot
from va_media.channels import CallFeed
from va_media.control import ControlHandler, DefaultControlHandler
from va_media.ownership import OWNER_TTL_MS, RENEW_INTERVAL_S, CallOwnership
from va_media.socket import MediaSocket
from va_telnyx.media import MediaFormat

__all__ = [
    "HEARTBEAT_INTERVAL_S",
    "HEARTBEAT_TTL_S",
    "OWNERSHIP_LOST",
    "STREAM_CLOSED",
    "CallSession",
    "SessionBody",
    "SessionEnd",
    "StreamStart",
]

HEARTBEAT_INTERVAL_S: Final = 3.0
HEARTBEAT_TTL_S: Final = 10
CLOSE_NORMAL: Final = 1000
CLOSE_INTERNAL_ERROR: Final = 1011

_log = get_logger(__name__)

SessionCause = Literal["stream_closed", "ownership_lost", "ended"]


@dataclass(frozen=True, slots=True)
class SessionEnd:
    """Why a session ended; ``label`` (the end reason, else the cause) is the ``end_reason`` metric label."""

    cause: SessionCause
    end_reason: str | None = None
    ended_by: str | None = None

    @property
    def label(self) -> str:
        return self.end_reason or self.cause


STREAM_CLOSED: Final = SessionEnd("stream_closed")
OWNERSHIP_LOST: Final = SessionEnd("ownership_lost", end_reason="worker_lost", ended_by="error")


@dataclass(frozen=True, slots=True)
class StreamStart:
    """The Telnyx ``start`` frame facts the session keeps."""

    stream_id: str
    call_control_id: str
    media_format: MediaFormat


type SessionBody = Callable[["CallSession"], Awaitable[SessionEnd]]


class CallSession:
    """The tasks of one attached call; :meth:`run` returns how it ended."""

    def __init__(  # noqa: PLR0913 - keyword-only collaborators
        self,
        *,
        context: CallContext,
        socket: MediaSocket,
        start: StreamStart,
        ownership: CallOwnership,
        redis: Redis,
        clock: Clock,
        metrics: ServiceMetrics,
        feed: CallFeed | None = None,
        carrier_state: CarrierSnapshot | None = None,
        control_handler: ControlHandler | None = None,
    ) -> None:
        self.call_id: uuid.UUID = context.call_id
        self.context = context
        self.socket = socket
        self.start = start
        self.feed = feed if feed is not None else CallFeed(context.call_id)
        self.carrier_state = carrier_state if carrier_state is not None else CarrierSnapshot()
        self._control_handler: ControlHandler = (
            control_handler if control_handler is not None else DefaultControlHandler()
        )
        self._acks: set[asyncio.Task[None]] = set()
        self._ownership = ownership
        self._redis = redis
        self._clock = clock
        self._metrics = metrics
        self._requested: SessionEnd | None = None
        self._end_requested = asyncio.Event()
        self._lost = False

    @property
    def ownership_lost(self) -> bool:
        return self._lost

    def request_end(self, end_reason: str, ended_by: str) -> None:
        """End the session with ``end_reason``/``ended_by`` (the first request wins)."""
        if self._requested is None:
            self._requested = SessionEnd("ended", end_reason=end_reason, ended_by=ended_by)
            self._end_requested.set()

    def notify(self, message: ControlMessage) -> None:
        """Deliver a message produced on this pod (the drain notice); it is handled but never acknowledged."""
        self.feed.deliver_local(message)

    async def run(self, body: SessionBody) -> SessionEnd:
        """Run ``body`` with the renewer, the heartbeat, and the control listener; a body or control handler
        exception propagates after cleanup."""
        with bound_context(org_id=str(self.context.org_id)):
            self._metrics.counter("va_media_calls_started_total").inc()
            _log.info("media.session_started", stream_id=self.start.stream_id)
            end: SessionEnd | None = None
            try:
                end = await self._race(body)
            finally:
                await self._cleanup(end)
            return end

    async def _race(self, body: SessionBody) -> SessionEnd:
        tasks = [
            asyncio.create_task(self._ownership_renewer(), name="ownership_renewer"),
            asyncio.create_task(self._wait_requested(), name="end_request"),
            asyncio.ensure_future(body(self)),
            asyncio.create_task(self._control_listener(), name="control_listener"),
            asyncio.create_task(self._heartbeat(), name="heartbeat"),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finished = [task for task in tasks if task in done]  # priority: lost, requested, body, control
        for task in finished:
            if task.exception() is not None:
                return task.result()
        return finished[0].result()

    async def _wait_requested(self) -> SessionEnd:
        await self._end_requested.wait()
        assert self._requested is not None  # noqa: S101 - set together with the event
        return self._requested

    async def _ownership_renewer(self) -> SessionEnd:
        last_renewed = self._clock.monotonic()
        while True:
            await self._clock.sleep(RENEW_INTERVAL_S)
            try:
                held = await self._ownership.renew()
            except (RedisError, OSError) as exc:
                unrenewed_s = self._clock.monotonic() - last_renewed
                _log.warning(
                    "media.ownership_renew_failed", detail=type(exc).__name__, unrenewed_s=unrenewed_s
                )
                if unrenewed_s * 1000 >= OWNER_TTL_MS:
                    self._lost = True
                    _log.error("media.ownership_lost", detail="renewal_failed")
                    return OWNERSHIP_LOST
                continue
            if not held:
                self._lost = True
                _log.error("media.ownership_lost", detail="taken_or_expired")
                return OWNERSHIP_LOST
            last_renewed = self._clock.monotonic()

    async def _control_listener(self) -> SessionEnd:
        while True:
            delivery = await self.feed.next_control()
            message = delivery.message
            handled = await self._control_handler.handle(self, message)
            if handled and not delivery.local:
                ack = asyncio.create_task(self._ack(message), name="control_ack")
                self._acks.add(ack)
                ack.add_done_callback(self._acks.discard)
                await asyncio.shield(ack)

    async def _ack(self, message: ControlMessage) -> None:
        try:
            await self._redis.set(control_ack_key(self.call_id, message.id), "1", ex=CONTROL_ACK_TTL_S)
        except (RedisError, OSError) as exc:
            _log.warning(
                "media.control_ack_failed",
                type=message.type,
                message_id=str(message.id),
                detail=type(exc).__name__,
            )

    async def _heartbeat(self) -> SessionEnd:
        key = heartbeat_key(self.call_id)
        while True:
            try:
                await self._redis.set(key, self._clock.now().isoformat(), ex=HEARTBEAT_TTL_S)
            except (RedisError, OSError) as exc:
                _log.warning("media.heartbeat_failed", detail=type(exc).__name__)
            await self._clock.sleep(HEARTBEAT_INTERVAL_S)

    async def _cleanup(self, end: SessionEnd | None) -> None:
        if self._acks:
            await asyncio.gather(*self._acks, return_exceptions=True)
        try:
            await self._redis.delete(heartbeat_key(self.call_id))
        except (RedisError, OSError) as exc:
            _log.warning("media.heartbeat_delete_failed", detail=type(exc).__name__)
        if not self._lost:
            try:
                await self._ownership.release()
            except (RedisError, OSError) as exc:
                _log.warning("media.ownership_release_failed", detail=type(exc).__name__)
        label = end.label if end is not None else "error"
        self._metrics.counter("va_media_calls_ended_total").labels(end_reason=label).inc()
        _log.info("media.session_ended", end_reason=label, cause=end.cause if end is not None else "error")
        await self.socket.close(CLOSE_NORMAL if end is not None else CLOSE_INTERNAL_ERROR)
