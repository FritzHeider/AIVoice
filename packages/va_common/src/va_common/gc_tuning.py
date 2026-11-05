"""Cyclic-collector tuning and pause log for real-time processes (specs/07-media-pipeline.md §9 Decisions
P08-FIX-3 and P08-FIX-4).

A collection runs synchronously on the event loop: in the media worker one longer than the outbound lead
(60 ms) underruns every leg that is speaking, and in the simulator runner it delays every scripted caller and
every agent frame it times. :func:`tune_gc` moves everything allocated at startup to the permanent generation
(no later pass walks it), sets the process's thresholds, and installs a :class:`GcPauseLog`, which times every
collection through :data:`gc.callbacks` so a loop stall can be attributed to the collector or ruled out.

The callback only records: a pass at or above ``threshold_ms`` is appended to a bounded buffer (no logging,
no allocation beyond one tuple, inside the collector). :meth:`GcPauseLog.run`, a supervised task, logs the
buffered passes as ``gc_pause`` once a second in an empty :mod:`contextvars` context, so a line never carries
the ``call_id``/``org_id`` of whichever call happened to trigger the collection; passes dropped by a full
buffer are reported as one ``gc_pause_overflow``.
"""

from __future__ import annotations

import asyncio
import contextvars
import gc
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, Final

from va_common.logging import get_logger

__all__ = [
    "FLUSH_INTERVAL_S",
    "GC_PAUSE_THRESHOLD_MS",
    "PAUSE_BUFFER",
    "GcPauseLog",
    "installed_pause_log",
    "tune_gc",
]

GC_PAUSE_THRESHOLD_MS: Final = 10.0
"""Collections at least this long are logged (half a 20 ms frame)."""

FLUSH_INTERVAL_S: Final = 1.0
""":meth:`GcPauseLog.run` logs the buffered passes this often."""

PAUSE_BUFFER: Final = 256
"""Passes buffered between flushes; older ones are dropped (and counted) beyond it."""

_log = get_logger(__name__)

type _Pause = tuple[object, float, object, object]

_installed: GcPauseLog | None = None


class GcPauseLog:
    """Times collections via :data:`gc.callbacks`; :meth:`install` once, :meth:`uninstall` in tests."""

    __slots__ = ("_clock", "_pending", "_started", "dropped", "pauses", "threshold_ms")

    def __init__(
        self,
        *,
        threshold_ms: float = GC_PAUSE_THRESHOLD_MS,
        clock: Callable[[], float] = time.perf_counter,
        capacity: int = PAUSE_BUFFER,
    ) -> None:
        self.threshold_ms = threshold_ms
        self.pauses = 0
        self.dropped = 0
        self._clock = clock
        self._started: float | None = None
        self._pending: deque[_Pause] = deque(maxlen=capacity)

    def install(self) -> None:
        if self._callback not in gc.callbacks:
            gc.callbacks.append(self._callback)

    def uninstall(self) -> None:
        if self._callback in gc.callbacks:
            gc.callbacks.remove(self._callback)

    def flush(self) -> int:
        """Log every buffered pass (and the overflow count) outside any bound context; returns the number of
        ``gc_pause`` lines."""
        return contextvars.Context().run(self._emit)

    async def run(
        self,
        *,
        interval_s: float = FLUSH_INTERVAL_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Flush every ``interval_s`` until cancelled, then flush once more."""
        try:
            while True:
                await sleep(interval_s)
                self.flush()
        finally:
            self.flush()

    def _emit(self) -> int:
        logged = 0
        while self._pending:
            generation, duration_ms, collected, uncollectable = self._pending.popleft()
            _log.warning(
                "gc_pause",
                generation=generation,
                duration_ms=duration_ms,
                collected=collected,
                uncollectable=uncollectable,
            )
            logged += 1
        if self.dropped:
            dropped, self.dropped = self.dropped, 0
            _log.warning("gc_pause_overflow", dropped=dropped)
        return logged

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
        pending = self._pending
        if pending.maxlen is not None and len(pending) >= pending.maxlen:
            self.dropped += 1
        pending.append(
            (info.get("generation"), round(duration_ms, 1), info.get("collected"), info.get("uncollectable"))
        )


def installed_pause_log() -> GcPauseLog | None:
    """The pause log installed by :func:`tune_gc` in this process (the service supervises its :meth:`run`)."""
    return _installed


def tune_gc(thresholds: tuple[int, int, int]) -> GcPauseLog:
    """Collect, freeze the survivors into the permanent generation, set ``thresholds`` (generation 0, 1, 2),
    and install a pause log (returned and remembered for :func:`installed_pause_log`; tests uninstall it,
    reset ``_installed`` and ``gc.unfreeze()``)."""
    global _installed  # noqa: PLW0603 (one pause log per process, installed at startup)
    gc.collect()
    gc.freeze()
    gc.set_threshold(*thresholds)
    pauses = GcPauseLog()
    pauses.install()
    _installed = pauses
    return pauses
