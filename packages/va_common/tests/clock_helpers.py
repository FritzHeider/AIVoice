"""Shared clock test helpers: a frozen clock that records its sleeps."""

from __future__ import annotations

from va_common.time import FrozenClock

__all__ = ["RecordingClock"]


class RecordingClock(FrozenClock):
    """A :class:`FrozenClock` that records every sleep (and advances by it)."""

    def __init__(self) -> None:
        super().__init__()
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await super().sleep(seconds)
