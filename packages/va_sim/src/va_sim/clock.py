"""Simulator clocks (specs/30-testing-and-evals.md §3.2 Clock).

``realtime`` follows the wall clock: 20 ms of audio takes 20 ms. ``accelerated`` runs on virtual time: a sleep
returns as soon as no earlier sleeper is pending, and virtual time jumps to the sleeper's deadline. Concurrent
sleepers wake in deadline order (ties in registration order), so long flows finish in milliseconds while their
virtual timestamps stay exactly as they would be in real time.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol

from va_common.time import Clock, ensure_utc, utcnow
from va_sim.settings import ClockMode

__all__ = ["DEFAULT_START", "AcceleratedClock", "RealtimeClock", "SimClock", "clock_for", "iso_z"]

# Virtual epoch of an accelerated clock unless a test picks another one.
DEFAULT_START: Final = datetime(2026, 1, 1, tzinfo=UTC)
# Event-loop turns an accelerated sleeper waits before virtual time moves, so tasks woken by the previous
# deadline can register their next sleep first (a sleep issued right after a wake-up needs one turn).
_SETTLE_ROUNDS: Final = 3


class SimClock(Clock, Protocol):
    """A :class:`va_common.time.Clock` that also reports its mode and elapsed simulator time."""

    @property
    def mode(self) -> ClockMode: ...

    def virtual_ms(self) -> int:
        """Milliseconds since the clock started."""
        ...


def _check_non_negative(seconds: float, what: str) -> None:
    if seconds < 0:
        msg = f"cannot {what} a negative duration: {seconds}"
        raise ValueError(msg)


class RealtimeClock:
    """Wall-clock time; sleeping really waits."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._started = monotonic()

    @property
    def mode(self) -> ClockMode:
        return "realtime"

    def now(self) -> datetime:
        return utcnow()

    def monotonic(self) -> float:
        return self._monotonic()

    def virtual_ms(self) -> int:
        return int((self._monotonic() - self._started) * 1000)

    async def sleep(self, seconds: float) -> None:
        _check_non_negative(seconds, "sleep")
        await asyncio.sleep(seconds)


class AcceleratedClock:
    """Virtual time that advances only when every runnable task waits on this clock (or on ``advance``)."""

    def __init__(self, start: datetime = DEFAULT_START) -> None:
        self._start = ensure_utc(start)
        self._elapsed = 0.0
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._order = itertools.count()
        self._pump: asyncio.Task[None] | None = None

    @property
    def mode(self) -> ClockMode:
        return "accelerated"

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self._elapsed)

    def monotonic(self) -> float:
        return self._elapsed

    def virtual_ms(self) -> int:
        return round(self._elapsed * 1000)

    def advance(self, seconds: float) -> None:
        """Move virtual time forward by ``seconds`` and wake every sleeper whose deadline has passed."""
        _check_non_negative(seconds, "advance by")
        self._elapsed += seconds
        while self._sleepers and self._sleepers[0][0] <= self._elapsed:
            _, _, waiter = heapq.heappop(self._sleepers)
            if not waiter.done():
                waiter.set_result(None)

    async def sleep(self, seconds: float) -> None:
        _check_non_negative(seconds, "sleep")
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()
        heapq.heappush(self._sleepers, (self._elapsed + seconds, next(self._order), waiter))
        if self._pump is None or self._pump.done():
            self._pump = loop.create_task(self._run_pump(), name="sim-clock-pump")
        await waiter

    async def _run_pump(self) -> None:
        while self._sleepers:
            for _ in range(_SETTLE_ROUNDS):
                await asyncio.sleep(0)
            if not self._sleepers:
                break
            deadline, _, waiter = heapq.heappop(self._sleepers)
            if waiter.done():
                continue
            self._elapsed = max(self._elapsed, deadline)
            waiter.set_result(None)


def clock_for(mode: ClockMode) -> SimClock:
    """A fresh clock of ``mode``."""
    if mode == "accelerated":
        return AcceleratedClock()
    return RealtimeClock()


def iso_z(moment: datetime) -> str:
    """``moment`` as ISO-8601 UTC with a ``Z`` suffix (the timestamp format of every simulator response)."""
    return ensure_utc(moment).isoformat().replace("+00:00", "Z")
