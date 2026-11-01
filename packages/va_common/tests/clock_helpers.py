"""Shared clock test helpers: a frozen clock that records its sleeps, and a manual clock whose sleepers wake
only when the test advances time."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from va_common.time import FrozenClock

__all__ = ["ManualClock", "RecordingClock", "settle"]

_SETTLE_TURNS = 20
_EPSILON = 1e-9


class RecordingClock(FrozenClock):
    """A :class:`FrozenClock` that records every sleep (and advances by it)."""

    def __init__(self) -> None:
        super().__init__()
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await super().sleep(seconds)


async def settle(turns: int = _SETTLE_TURNS) -> None:
    """Let ready callbacks run for ``turns`` event-loop iterations."""
    for _ in range(turns):
        await asyncio.sleep(0)


@dataclass(eq=False, slots=True)
class _Sleeper:
    deadline: float
    future: asyncio.Future[None] = field(default_factory=lambda: asyncio.get_running_loop().create_future())


class ManualClock(FrozenClock):
    """A frozen clock whose ``sleep`` parks the caller until :meth:`run_for` moves time past its deadline.

    ``sleep(0)`` (or a negative delay) yields once. Cancelling a sleeper removes it.
    """

    def __init__(self) -> None:
        super().__init__()
        self._sleepers: list[_Sleeper] = []
        self._parked = asyncio.Event()

    @property
    def sleepers(self) -> int:
        """Sleepers still parked."""
        return len(self._sleepers)

    def next_deadline(self) -> float | None:
        return min((sleeper.deadline for sleeper in self._sleepers), default=None)

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        sleeper = _Sleeper(self.monotonic() + seconds)
        self._sleepers.append(sleeper)
        self._parked.set()
        try:
            await sleeper.future
        finally:
            if sleeper in self._sleepers:
                self._sleepers.remove(sleeper)

    async def wait_for_sleep(self, seconds: float, *, timeout_s: float = 5.0) -> None:
        """Wait (wall-clock bounded) until some caller sleeps exactly ``seconds`` from now."""
        async with asyncio.timeout(timeout_s):
            while not any(abs(s.deadline - self.monotonic() - seconds) < _EPSILON for s in self._sleepers):
                self._parked.clear()
                await self._parked.wait()

    async def run_for(self, seconds: float) -> None:
        """Advance ``seconds``, waking sleepers deadline by deadline (with settled turns in between)."""
        target = self.monotonic() + seconds
        await settle()
        while True:
            due = self.next_deadline()
            if due is None or due > target:
                break
            if due > self.monotonic():
                self.advance(due - self.monotonic())
            for sleeper in [s for s in self._sleepers if s.deadline <= self.monotonic()]:
                self._sleepers.remove(sleeper)
                if not sleeper.future.done():
                    sleeper.future.set_result(None)
            await settle()
        if target > self.monotonic():
            self.advance(target - self.monotonic())
        await settle()
