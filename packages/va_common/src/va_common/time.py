"""Timezone-aware UTC time and an injectable clock.

Every timestamp in the platform is an aware UTC ``datetime``. Code that needs the current time, a monotonic
reading, or to sleep takes a :class:`Clock` so tests can substitute :class:`FrozenClock` instead of sleeping.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol

__all__ = ["Clock", "FrozenClock", "SystemClock", "ensure_utc", "utcnow"]

DEFAULT_FROZEN_START = datetime(2026, 1, 1, tzinfo=UTC)


def utcnow() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """Convert an aware datetime to UTC; reject naive datetimes."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        msg = f"naive datetime is not allowed: {dt.isoformat()}"
        raise ValueError(msg)
    return dt.astimezone(UTC)


class Clock(Protocol):
    """Source of wall-clock time, monotonic time, and sleeping."""

    def now(self) -> datetime:
        """Current wall-clock time as an aware UTC datetime."""
        ...

    def monotonic(self) -> float:
        """Monotonic reading in seconds; only differences are meaningful."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Suspend the caller for ``seconds``."""
        ...


class SystemClock:
    """Real clock backed by the operating system and the running event loop."""

    def now(self) -> datetime:
        return utcnow()

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FrozenClock:
    """Deterministic clock for tests: time only moves when told to."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = ensure_utc(start) if start is not None else DEFAULT_FROZEN_START
        self._monotonic = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, delta: float | timedelta) -> None:
        """Move both wall-clock and monotonic time forward by ``delta`` (seconds or timedelta)."""
        seconds = delta.total_seconds() if isinstance(delta, timedelta) else float(delta)
        if seconds < 0:
            msg = f"cannot advance a clock backwards: {seconds}"
            raise ValueError(msg)
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds

    def set(self, dt: datetime) -> None:
        """Jump the wall clock to ``dt``; monotonic time is unaffected (like an NTP step)."""
        self._now = ensure_utc(dt)

    async def sleep(self, seconds: float) -> None:
        """Advance time by ``seconds`` immediately, then yield once to the event loop."""
        self.advance(seconds)
        await asyncio.sleep(0)
