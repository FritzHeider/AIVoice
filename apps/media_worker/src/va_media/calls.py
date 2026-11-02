"""Per-pod call admission (specs/07-media-pipeline.md §1: ``MEDIA_MAX_CALLS_PER_POD`` admission control).

Admission refuses a stream only at ``MEDIA_MAX_CALLS_PER_POD``; the readiness check ``capacity`` already
fails at 90 % of it (:attr:`CallRegistry.ready_limit`) so the load balancer stops routing new streams to the
pod well before the hard limit (Telnyx does not retry a refused stream).
"""

from __future__ import annotations

import asyncio

from prometheus_client import Gauge

__all__ = ["CallRegistry", "CapacityError"]


class CapacityError(RuntimeError):
    """The pod holds at least 90 % of ``MEDIA_MAX_CALLS_PER_POD`` calls (readiness check failure)."""


class CallRegistry:
    """Calls attached to this pod; admission is refused at capacity or for a call already attached.

    ``gauge`` (the ``va_media_active_calls`` child of the transport) follows the number of attached calls.
    """

    def __init__(self, max_calls: int, *, gauge: Gauge | None = None) -> None:
        if max_calls < 1:
            msg = "max_calls must be at least 1"
            raise ValueError(msg)
        self.max_calls = max_calls
        self._calls: set[str] = set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._gauge = gauge
        self._publish()

    @property
    def active(self) -> int:
        return len(self._calls)

    @property
    def at_capacity(self) -> bool:
        return self.active >= self.max_calls

    @property
    def ready_limit(self) -> int:
        """``ceil(0.9 * max_calls)``: readiness fails from this many attached calls on (54 of 60, 1 of 1)."""
        return (self.max_calls * 9 + 9) // 10

    def contains(self, call_id: str) -> bool:
        """Whether ``call_id`` is attached to this pod."""
        return call_id in self._calls

    def admit(self, call_id: str) -> bool:
        """Attach ``call_id``; ``False`` when the pod is full or the call is already attached."""
        if call_id in self._calls or self.at_capacity:
            return False
        self._calls.add(call_id)
        self._idle.clear()
        self._publish()
        return True

    def release(self, call_id: str) -> None:
        """Detach ``call_id`` (no-op when it is not attached)."""
        self._calls.discard(call_id)
        if not self._calls:
            self._idle.set()
        self._publish()

    def _publish(self) -> None:
        if self._gauge is not None:
            self._gauge.set(self.active)

    async def wait_idle(self, timeout_s: float) -> bool:
        """Wait until no call is attached; ``False`` when ``timeout_s`` elapses first."""
        try:
            async with asyncio.timeout(timeout_s):
                await self._idle.wait()
        except TimeoutError:
            return False
        return True

    async def capacity_check(self) -> None:
        """Readiness check ``capacity``: fails from :attr:`ready_limit` attached calls on."""
        if self.active >= self.ready_limit:
            msg = f"{self.active} of {self.max_calls} calls attached (readiness limit {self.ready_limit})"
            raise CapacityError(msg)
