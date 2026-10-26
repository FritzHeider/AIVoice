"""Per-pod call admission (specs/01-architecture.md §3: ``MEDIA_MAX_CALLS_PER_POD`` admission control)."""

from __future__ import annotations

import asyncio

__all__ = ["CallRegistry", "CapacityError"]


class CapacityError(RuntimeError):
    """The pod holds ``MEDIA_MAX_CALLS_PER_POD`` calls (readiness check failure)."""


class CallRegistry:
    """Calls attached to this pod; admission is refused at capacity or for a call already attached."""

    def __init__(self, max_calls: int) -> None:
        if max_calls < 1:
            msg = "max_calls must be at least 1"
            raise ValueError(msg)
        self.max_calls = max_calls
        self._calls: set[str] = set()
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def active(self) -> int:
        return len(self._calls)

    @property
    def at_capacity(self) -> bool:
        return self.active >= self.max_calls

    def admit(self, call_id: str) -> bool:
        """Attach ``call_id``; ``False`` when the pod is full or the call is already attached."""
        if call_id in self._calls or self.at_capacity:
            return False
        self._calls.add(call_id)
        self._idle.clear()
        return True

    def release(self, call_id: str) -> None:
        """Detach ``call_id`` (no-op when it is not attached)."""
        self._calls.discard(call_id)
        if not self._calls:
            self._idle.set()

    async def wait_idle(self, timeout_s: float) -> bool:
        """Wait until no call is attached; ``False`` when ``timeout_s`` elapses first."""
        try:
            async with asyncio.timeout(timeout_s):
                await self._idle.wait()
        except TimeoutError:
            return False
        return True

    async def capacity_check(self) -> None:
        """Readiness check ``capacity``: fails while the pod is full."""
        if self.at_capacity:
            msg = f"{self.active} of {self.max_calls} calls attached"
            raise CapacityError(msg)
