"""Per-call control and carrier channels over one pod-wide Redis pub/sub connection
(specs/07-media-pipeline.md §3 step 6, §17; specs/07 §3 Decision P05-T07b).

A pub/sub connection per session would hold one pooled connection per call (60 calls exhaust the default
pool), so the pod keeps ONE :class:`ChannelHub`: :meth:`ChannelHub.subscribe` subscribes
``call:{<id>}:control`` and ``call:{<id>}:carrier`` and returns a :class:`CallFeed` once Redis confirmed both
subscriptions (2 s on the injected clock, else :class:`ChannelError`). The supervised :meth:`ChannelHub.run`
task is the only reader of the connection; it routes each message to its feed.

A :class:`CallFeed` queues typed control messages (unbounded; invalid ones are dropped with a warning) and
carrier events (bounded at 256, the oldest dropped on overflow; malformed events, another call's events, and
redeliveries of one of the last 512 ``telnyx_event_id`` values are dropped).
"""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError

from va_common.logging import get_logger
from va_common.time import Clock
from va_domain.carrier import CarrierEvent, carrier_channel
from va_domain.control import ControlMessage, ControlMessageError, control_channel, parse_control
from va_voice.pipeline.waits import within

__all__ = [
    "CARRIER_DEDUPE_SIZE",
    "CARRIER_QUEUE_SIZE",
    "CONFIRM_TIMEOUT_S",
    "CallFeed",
    "ChannelError",
    "ChannelHub",
    "ControlDelivery",
]

CONFIRM_TIMEOUT_S: Final = 2.0
CARRIER_QUEUE_SIZE: Final = 256
CARRIER_DEDUPE_SIZE: Final = 512
READ_TIMEOUT_S: Final = 5.0
"""Socket read timeout of one ``get_message`` (an idle wait, not a failure: the loop reads again)."""
RETRY_DELAY_S: Final = 1.0

_log = get_logger(__name__)

ChannelKind = Literal["control", "carrier"]


class ChannelError(Exception):
    """The call's channels could not be subscribed (``reason``: ``redis`` or ``timeout``)."""

    def __init__(self, reason: Literal["redis", "timeout"]) -> None:
        super().__init__(f"channel subscription failed: {reason}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ControlDelivery:
    """A control message and whether it came from this pod (``local``: never acknowledged in Redis)."""

    message: ControlMessage
    local: bool = False


def _text(value: Any) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)


class CallFeed:
    """The control and carrier messages of one subscribed call."""

    def __init__(self, call_id: uuid.UUID) -> None:
        self.call_id = call_id
        self.channels: Final = (control_channel(call_id), carrier_channel(call_id))
        self._control: asyncio.Queue[ControlDelivery] = asyncio.Queue()
        self._carrier: deque[CarrierEvent] = deque(maxlen=CARRIER_QUEUE_SIZE)
        self._carrier_ready = asyncio.Event()
        self._seen: OrderedDict[str, None] = OrderedDict()

    async def next_control(self) -> ControlDelivery:
        return await self._control.get()

    async def next_carrier(self) -> CarrierEvent:
        while not self._carrier:
            self._carrier_ready.clear()
            await self._carrier_ready.wait()
        return self._carrier.popleft()

    def deliver_local(self, message: ControlMessage) -> None:
        """Queue a message produced on this pod (the drain notice)."""
        self._control.put_nowait(ControlDelivery(message, local=True))

    def on_control(self, raw: bytes | str) -> None:
        try:
            message = parse_control(raw)
        except ControlMessageError as exc:
            _log.warning("media.control_invalid", call_id=str(self.call_id), errors=exc.errors)
            return
        self._control.put_nowait(ControlDelivery(message))

    def on_carrier(self, raw: bytes | str) -> None:
        try:
            event = CarrierEvent.model_validate_json(raw)
        except ValidationError as exc:
            _log.warning(
                "media.carrier_invalid",
                call_id=str(self.call_id),
                detail="malformed",
                errors=exc.error_count(),
            )
            return
        if event.call_id != self.call_id:
            _log.warning("media.carrier_invalid", call_id=str(self.call_id), detail="other_call")
            return
        if event.telnyx_event_id in self._seen:
            return
        self._seen[event.telnyx_event_id] = None
        if len(self._seen) > CARRIER_DEDUPE_SIZE:
            self._seen.popitem(last=False)
        if len(self._carrier) == CARRIER_QUEUE_SIZE:
            dropped = self._carrier[0]
            _log.warning("media.carrier_overflow", call_id=str(self.call_id), dropped_type=dropped.type)
        self._carrier.append(event)
        self._carrier_ready.set()


class ChannelHub:
    """The pod's pub/sub connection for every attached call's channels; run :meth:`run` supervised."""

    def __init__(self, redis: Redis, clock: Clock, *, confirm_timeout_s: float = CONFIRM_TIMEOUT_S) -> None:
        self._pubsub: PubSub = redis.pubsub()
        self._clock = clock
        self._confirm_timeout_s = confirm_timeout_s
        self._lock = asyncio.Lock()
        self._routes: dict[str, tuple[CallFeed, ChannelKind]] = {}
        self._confirmed: dict[str, asyncio.Event] = {}
        self._wake = asyncio.Event()

    @property
    def subscribed_channels(self) -> frozenset[str]:
        return frozenset(self._routes)

    async def subscribe(self, call_id: uuid.UUID) -> CallFeed:
        """Subscribe the call's channels; returns once Redis confirmed both. Raises :class:`ChannelError`."""
        feed = CallFeed(call_id)
        control, carrier = feed.channels
        pairs: tuple[tuple[str, ChannelKind], ...] = ((control, "control"), (carrier, "carrier"))
        async with self._lock:
            waits = []
            for channel, kind in pairs:
                self._routes[channel] = (feed, kind)
                self._confirmed[channel] = asyncio.Event()
                waits.append(self._confirmed[channel].wait())
            try:
                await self._pubsub.subscribe(*feed.channels)
            except (RedisError, OSError) as exc:
                for wait in waits:
                    wait.close()
                self._forget(feed)
                _log.warning(
                    "media.channels_subscribe_failed", call_id=str(call_id), detail=type(exc).__name__
                )
                raise ChannelError("redis") from exc
            self._wake.set()
        if not await within(asyncio.gather(*waits), self._clock, self._confirm_timeout_s):
            _log.warning("media.channels_subscribe_failed", call_id=str(call_id), detail="timeout")
            await self.unsubscribe(feed)
            raise ChannelError("timeout")
        return feed

    async def unsubscribe(self, feed: CallFeed) -> None:
        """Stop routing the call's channels and unsubscribe them (a Redis error is logged)."""
        async with self._lock:
            self._forget(feed)
            try:
                await self._pubsub.unsubscribe(*feed.channels)
            except (RedisError, OSError) as exc:
                _log.warning(
                    "media.channels_unsubscribe_failed", call_id=str(feed.call_id), detail=type(exc).__name__
                )

    def _forget(self, feed: CallFeed) -> None:
        for channel in feed.channels:
            route = self._routes.get(channel)
            if route is not None and route[0] is feed:
                del self._routes[channel]
                self._confirmed.pop(channel, None)

    async def run(self) -> None:
        """Read the connection while anything is subscribed; Redis errors are logged and retried after 1 s.

        After a reconnect redis-py re-subscribes every channel still subscribed (its ``on_connect`` callback).
        """
        while True:
            await self._wake.wait()
            if not self._pubsub.subscribed:
                self._wake.clear()
                continue
            try:
                message = await self._pubsub.get_message(timeout=READ_TIMEOUT_S)
            except (RedisError, OSError) as exc:
                _log.warning("media.channels_failed", detail=type(exc).__name__)
                await self._clock.sleep(RETRY_DELAY_S)
                continue
            if message is not None:
                self._dispatch(message)

    def _dispatch(self, message: dict[str, Any]) -> None:
        channel = _text(message.get("channel"))
        kind = message.get("type")
        if kind == "subscribe":
            confirmed = self._confirmed.get(channel)
            if confirmed is not None:
                confirmed.set()
            return
        if kind != "message":
            return
        route = self._routes.get(channel)
        if route is None:
            return
        feed, channel_kind = route
        data = message.get("data")
        if not isinstance(data, bytes | str):
            return
        if channel_kind == "control":
            feed.on_control(data)
        else:
            feed.on_carrier(data)

    async def aclose(self) -> None:
        """Close the pub/sub connection (at shutdown, after :meth:`run` stopped)."""
        try:
            await self._pubsub.aclose()  # type: ignore[no-untyped-call]
        except (RedisError, OSError) as exc:
            _log.warning("media.channels_close_failed", detail=type(exc).__name__)
