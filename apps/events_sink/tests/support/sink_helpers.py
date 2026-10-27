"""Events sink test helpers: scripted ClickHouse connections and a clock that records its sleeps."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from ch_support import FakeClickHouse

from va_common.time import FrozenClock

__all__ = ["FakeConnect", "RecordingClock", "never_connects"]


@dataclass
class FakeConnect:
    """``SchemaConnect`` that raises the queued ``failures`` one per call, then yields ``client``."""

    client: FakeClickHouse = field(default_factory=FakeClickHouse)
    failures: list[Exception] = field(default_factory=list)
    opened: int = 0
    closed: int = 0

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[FakeClickHouse]:
        self.opened += 1
        if self.failures:
            raise self.failures.pop(0)
        try:
            yield self.client
        finally:
            self.closed += 1


@asynccontextmanager
async def never_connects() -> AsyncIterator[FakeClickHouse]:
    """A connection attempt that hangs until the lifespan cancels it."""
    await asyncio.Event().wait()
    yield FakeClickHouse()


class RecordingClock(FrozenClock):
    def __init__(self) -> None:
        super().__init__()
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await super().sleep(seconds)
