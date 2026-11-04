"""Cyclic-collector tuning and pause log for real-time processes (specs/07-media-pipeline.md §9 Decision
P08-FIX-3).

A collection runs synchronously on the event loop: in the media worker one longer than the outbound lead
(60 ms) underruns every leg that is speaking, and in the simulator runner it delays every scripted caller and
every agent frame it times. :func:`tune_gc` moves everything allocated at startup to the permanent generation
(no later pass walks it), sets the process's thresholds, and installs a :class:`GcPauseLog`, which times every
collection through :data:`gc.callbacks` and logs ``gc_pause`` (generation, duration, objects collected) for
the ones at or above ``threshold_ms``, so a loop stall can be attributed to the collector or ruled out.
"""

from __future__ import annotations

import gc
import time
from collections.abc import Callable
from typing import Any, Final

from va_common.logging import get_logger

__all__ = ["GC_PAUSE_THRESHOLD_MS", "GcPauseLog", "tune_gc"]

GC_PAUSE_THRESHOLD_MS: Final = 10.0
"""Collections at least this long are logged (half a 20 ms frame)."""

_log = get_logger(__name__)


class GcPauseLog:
    """Times collections via :data:`gc.callbacks`; :meth:`install` once, :meth:`uninstall` in tests."""

    __slots__ = ("_clock", "_started", "pauses", "threshold_ms")

    def __init__(
        self, *, threshold_ms: float = GC_PAUSE_THRESHOLD_MS, clock: Callable[[], float] = time.perf_counter
    ) -> None:
        self.threshold_ms = threshold_ms
        self.pauses = 0
        self._clock = clock
        self._started: float | None = None

    def install(self) -> None:
        if self._callback not in gc.callbacks:
            gc.callbacks.append(self._callback)

    def uninstall(self) -> None:
        if self._callback in gc.callbacks:
            gc.callbacks.remove(self._callback)

    def _callback(self, phase: str, info: dict[str, Any]) -> None:
        if phase == "start":
            self._started = self._clock()
            return
        started, self._started = self._started, None
        if started is None:
            return
        duration_ms = (self._clock() - started) * 1000.0
        if duration_ms < self.threshold_ms:
            return
        self.pauses += 1
        _log.warning(
            "gc_pause",
            generation=info.get("generation"),
            duration_ms=round(duration_ms, 1),
            collected=info.get("collected"),
            uncollectable=info.get("uncollectable"),
        )


def tune_gc(thresholds: tuple[int, int, int]) -> GcPauseLog:
    """Collect, freeze the survivors into the permanent generation, set ``thresholds`` (generation 0, 1, 2),
    and install a pause log (returned; tests uninstall it and ``gc.unfreeze()``)."""
    gc.collect()
    gc.freeze()
    gc.set_threshold(*thresholds)
    pauses = GcPauseLog()
    pauses.install()
    return pauses
