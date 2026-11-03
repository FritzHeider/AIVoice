"""Fan-out of live pub/sub channels to many in-process subscribers (specs/19-events-and-webhooks.md §9;
Decision P06-T06a).

Every SSE client of a call tails ``call:{<id>}:live``. A pub/sub connection per client would hold one pooled
Redis connection each, so a process keeps ONE :class:`LiveHub`: the first :meth:`LiveHub.subscribe` of a
channel sends ``SUBSCRIBE`` and waits for Redis to confirm it (``confirm_timeout_s`` on the injected clock),
later subscribers of the same channel join the existing subscription, and the last
:meth:`LiveHub.unsubscribe` sends ``UNSUBSCRIBE``.

The hub owns one reader task, started with the first subscription and cancelled by :meth:`LiveHub.aclose`;
it is the only reader of the connection and copies each message into every subscriber's queue. A
subscriber whose queue already holds ``queue_size`` messages is closed with reason ``overflow`` (it fell
behind; the client resumes from its last seq). A Redis error while reading closes every subscription with
``lost`` and drops the connection; the next subscribe opens a fresh one.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from typing import Any, Final, Literal

from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError

from va_common.logging import get_logger
from va_common.time import Clock
from va_common.waits import wait_until_set

__all__ = [
    "CONFIRM_TIMEOUT_S",
    "QUEUE_SIZE",
    "CloseReason",
    "LiveHub",
    "LiveHubError",
    "LiveSubscription",
    "LiveSubscriptionClosedError",
]

CONFIRM_TIMEOUT_S: Final = 2.0
QUEUE_SIZE: Final = 1_000
READ_TIMEOUT_S: Final = 5.0
"""Socket read timeout of one ``get_message`` (an idle wait: the reader simply reads again)."""

CloseReason = Literal["overflow", "lost", "closed"]

_log = get_logger(__name__)


class LiveHubError(Exception):
    """A channel could not be subscribed (``reason``: ``redis`` or ``timeout``)."""

    def __init__(self, reason: Literal["redis", "timeout"]) -> None:
        super().__init__(f"live channel subscription failed: {reason}")
        self.reason = reason


class LiveSubscriptionClosedError(Exception):
    """The subscription ended (``reason``: ``overflow``, ``lost``, or ``closed``)."""

    def __init__(self, reason: CloseReason) -> None:
        super().__init__(f"live subscription closed: {reason}")
        self.reason: CloseReason = reason


class LiveSubscription:
    """One subscriber's queue of messages from :attr:`channel`; read it with :meth:`next`."""

    def __init__(self, channel: str, queue_size: int) -> None:
        self.channel = channel
        self._queue_size = queue_size
        self._messages: deque[bytes] = deque()
        self._ready = asyncio.Event()
        self._closed: CloseReason | None = None

    @property
    def closed_reason(self) -> CloseReason | None:
        """Why the subscription ended; ``None`` while open."""
        return self._closed

    async def next(self) -> bytes:
        """The next message; raises :class:`LiveSubscriptionClosedError` once the queue is empty and it ended.

        Messages queued before an ``overflow``/``lost`` close are still returned first.
        """
        while not self._messages:
            if self._closed is not None:
                raise LiveSubscriptionClosedError(self._closed)
            self._ready.clear()
            await self._ready.wait()
        return self._messages.popleft()

    def deliver(self, message: bytes) -> None:
        """Queue ``message``; a full queue closes the subscription with ``overflow``."""
        if self._closed is not None:
            return
        if len(self._messages) >= self._queue_size:
            self._messages.clear()
            self.close("overflow")
            return
        self._messages.append(message)
        self._ready.set()

    def close(self, reason: CloseReason) -> None:
        """End the subscription (the first reason wins)."""
        if self._closed is None:
            self._closed = reason
        self._ready.set()


def _text(value: Any) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)


class LiveHub:
    """The process's pub/sub connection for live channels, shared by every subscriber."""

    def __init__(
        self,
        redis: Redis,
        clock: Clock,
        *,
        confirm_timeout_s: float = CONFIRM_TIMEOUT_S,
        queue_size: int = QUEUE_SIZE,
    ) -> None:
        self._redis = redis
        self._clock = clock
        self._confirm_timeout_s = confirm_timeout_s
        self._queue_size = queue_size
        self._lock = asyncio.Lock()
        self._pubsub: PubSub | None = None
        self._reader: asyncio.Task[None] | None = None
        self._members: dict[str, set[LiveSubscription]] = {}
        self._confirmed: dict[str, asyncio.Event] = {}
        self._closed = False

    @property
    def subscribed_channels(self) -> frozenset[str]:
        """Channels with at least one subscriber."""
        return frozenset(self._members)

    def subscribers(self, channel: str) -> int:
        """How many subscribers ``channel`` has."""
        return len(self._members.get(channel, ()))

    async def subscribe(self, channel: str) -> LiveSubscription:
        """A subscription of ``channel``, returned once Redis confirmed it. Raises :class:`LiveHubError`."""
        subscription = LiveSubscription(channel, self._queue_size)
        async with self._lock:
            if self._closed:
                raise LiveHubError("redis")
            members = self._members.get(channel)
            if members is not None:
                members.add(subscription)
                confirmed = self._confirmed[channel]
            else:
                confirmed = await self._subscribe_channel(channel, subscription)
        if not await wait_until_set(confirmed, self._clock, self._confirm_timeout_s):
            _log.warning("events.live_subscribe_failed", channel=channel, detail="timeout")
            await self.unsubscribe(subscription)
            raise LiveHubError("timeout")
        if subscription.closed_reason is not None:
            raise LiveHubError("redis")
        return subscription

    async def _subscribe_channel(self, channel: str, subscription: LiveSubscription) -> asyncio.Event:
        if self._pubsub is None:
            self._pubsub = self._redis.pubsub()
        confirmed = asyncio.Event()
        self._members[channel] = {subscription}
        self._confirmed[channel] = confirmed
        try:
            await self._pubsub.subscribe(channel)
        except (RedisError, OSError) as exc:
            del self._members[channel]
            del self._confirmed[channel]
            _log.warning("events.live_subscribe_failed", channel=channel, detail=type(exc).__name__)
            raise LiveHubError("redis") from exc
        if self._reader is None or self._reader.done():
            self._reader = asyncio.create_task(self._read(self._pubsub), name="live-hub-reader")
        return confirmed

    async def unsubscribe(self, subscription: LiveSubscription) -> None:
        """Remove ``subscription`` (closed with ``closed``); the channel's last one sends ``UNSUBSCRIBE``."""
        subscription.close("closed")
        async with self._lock:
            members = self._members.get(subscription.channel)
            if members is None or subscription not in members:
                return
            members.discard(subscription)
            if members:
                return
            del self._members[subscription.channel]
            self._confirmed.pop(subscription.channel, None)
            if self._pubsub is None:
                return
            try:
                await self._pubsub.unsubscribe(subscription.channel)
            except (RedisError, OSError) as exc:
                _log.warning(
                    "events.live_unsubscribe_failed", channel=subscription.channel, detail=type(exc).__name__
                )

    async def _read(self, pubsub: PubSub) -> None:
        try:
            while True:
                if not pubsub.subscribed:
                    # Nothing subscribed (the last UNSUBSCRIBE was confirmed): the next subscribe restarts us.
                    return
                message = await pubsub.get_message(timeout=READ_TIMEOUT_S)
                if message is not None:
                    self._dispatch(message)
        except (RedisError, OSError) as exc:
            _log.warning("events.live_hub_lost", detail=type(exc).__name__)
            await self._lose(pubsub)

    def _dispatch(self, message: dict[str, Any]) -> None:
        match message.get("type"), _text(message.get("channel")), message.get("data"):
            case "subscribe", channel, _:
                if (confirmed := self._confirmed.get(channel)) is not None:
                    confirmed.set()
            case "message", channel, bytes() | str() as payload:
                raw = payload.encode() if isinstance(payload, str) else payload
                for subscription in tuple(self._members.get(channel, ())):
                    subscription.deliver(raw)
            case _:
                pass

    async def _lose(self, pubsub: PubSub) -> None:
        async with self._lock:
            if self._pubsub is not pubsub:
                return
            self._end_all("lost")
            self._pubsub = None
        await self._close_pubsub(pubsub)

    def _end_all(self, reason: CloseReason) -> None:
        for members in self._members.values():
            for subscription in members:
                subscription.close(reason)
        for confirmed in self._confirmed.values():
            confirmed.set()
        self._members.clear()
        self._confirmed.clear()

    @staticmethod
    async def _close_pubsub(pubsub: PubSub) -> None:
        try:
            await pubsub.aclose()  # type: ignore[no-untyped-call]
        except (RedisError, OSError) as exc:
            _log.warning("events.live_hub_close_failed", detail=type(exc).__name__)

    async def aclose(self) -> None:
        """Stop the reader, close every subscription with ``closed``, and close the connection."""
        async with self._lock:
            self._closed = True
            reader, self._reader = self._reader, None
            pubsub, self._pubsub = self._pubsub, None
            self._end_all("closed")
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        if pubsub is not None:
            await self._close_pubsub(pubsub)
