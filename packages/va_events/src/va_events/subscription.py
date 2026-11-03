"""The bounded in-process subscriber queue shared by the live hubs (specs/19-events-and-webhooks.md §9;
Decisions P06-T06a and P06-T06c).

A hub's single reader task :meth:`Subscription.deliver`\\ s each message into every interested subscriber's
queue; the subscriber reads with :meth:`Subscription.next`. A subscriber whose queue already holds
``queue_size`` messages is closed with ``overflow`` (it fell behind), a hub that loses Redis closes it with
``lost``, and an unsubscribe or hub shutdown closes it with ``closed``. The first reason wins.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Literal

__all__ = ["CloseReason", "Subscription", "SubscriptionClosedError"]

CloseReason = Literal["overflow", "lost", "closed"]


class SubscriptionClosedError(Exception):
    """The subscription ended (``reason``: ``overflow``, ``lost``, or ``closed``)."""

    def __init__(self, reason: CloseReason) -> None:
        super().__init__(f"live subscription closed: {reason}")
        self.reason: CloseReason = reason


class Subscription[T]:
    """One subscriber's queue of messages; read it with :meth:`next`."""

    def __init__(self, queue_size: int) -> None:
        self._queue_size = queue_size
        self._messages: deque[T] = deque()
        self._ready = asyncio.Event()
        self._closed: CloseReason | None = None

    @property
    def closed_reason(self) -> CloseReason | None:
        """Why the subscription ended; ``None`` while open."""
        return self._closed

    async def next(self) -> T:
        """The next message; raises :class:`SubscriptionClosedError` once the queue is empty and it ended.

        Messages queued before an ``overflow``/``lost`` close are still returned first.
        """
        while not self._messages:
            if self._closed is not None:
                raise SubscriptionClosedError(self._closed)
            self._ready.clear()
            await self._ready.wait()
        return self._messages.popleft()

    def deliver(self, message: T) -> None:
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
