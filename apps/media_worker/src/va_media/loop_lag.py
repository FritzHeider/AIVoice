"""Event loop lag monitor (specs/27-observability.md §3: ``va_event_loop_lag_ms``; §9: liveness < 500 ms)."""

from __future__ import annotations

import asyncio
from typing import Final

from prometheus_client import Histogram

__all__ = ["LAG_REASON", "LoopLagMonitor"]

LAG_REASON: Final = "event_loop_lag"


class LoopLagMonitor:
    """A ticker sleeping ``interval_s``; the overshoot of each sleep is the loop lag."""

    def __init__(
        self, *, interval_s: float = 0.1, threshold_s: float = 0.5, histogram: Histogram | None = None
    ) -> None:
        self.interval_s = interval_s
        self.threshold_s = threshold_s
        self.lag_s = 0.0
        self.first_sample = asyncio.Event()
        self._histogram = histogram

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            await asyncio.sleep(self.interval_s)
            self.record(max(0.0, loop.time() - started - self.interval_s))

    def record(self, lag_s: float) -> None:
        self.lag_s = lag_s
        if self._histogram is not None:
            self._histogram.observe(lag_s * 1000.0)
        self.first_sample.set()

    @property
    def healthy(self) -> bool:
        return self.lag_s < self.threshold_s

    def liveness(self) -> str | None:
        """``event_loop_lag`` while the last measured lag is at or above the threshold."""
        return None if self.healthy else LAG_REASON
