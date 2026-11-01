"""The media worker's write-behind ``call_events`` buffer (specs/19-events-and-webhooks.md §1 step 4).

Rows are flushed in one batch when 200 are pending, every second (on the injected clock), when a call ends
(:meth:`CallEventBuffer.flush_call`), and on :meth:`CallEventBuffer.aclose`. Flushes are serialised. A failed
flush puts the rows that were not written back at the front (publish order kept) and the next trigger retries
them: only :attr:`CallEventWriteError.unwritten` after a partial failure (one org's failure never holds back
the others), the whole batch after any other error (writers are idempotent, so a re-sent row is harmless).
At most 10 000 rows are kept; beyond that the oldest are dropped with an ``events.call_events_dropped``
warning (a count, no payloads). Run :meth:`CallEventBuffer.run` under the owner's supervisor or task group.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from collections.abc import Sequence
from typing import Final

from va_common.logging import get_logger
from va_common.time import Clock
from va_events.call_events import CallEventRow, CallEventWriteError, CallEventWriter

__all__ = ["DEFAULT_FLUSH_INTERVAL_S", "DEFAULT_FLUSH_ROWS", "DEFAULT_MAX_PENDING", "CallEventBuffer"]

DEFAULT_FLUSH_ROWS: Final = 200
DEFAULT_FLUSH_INTERVAL_S: Final = 1.0
DEFAULT_MAX_PENDING: Final = 10_000

_log = get_logger(__name__)


class CallEventBuffer:
    """Collects ``call_events`` rows and writes them in batches through a :class:`CallEventWriter`."""

    def __init__(
        self,
        writer: CallEventWriter,
        *,
        clock: Clock,
        max_rows: int = DEFAULT_FLUSH_ROWS,
        interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        max_pending: int = DEFAULT_MAX_PENDING,
    ) -> None:
        if not 0 < max_rows <= max_pending:
            msg = f"need 0 < max_rows <= max_pending, got {max_rows} and {max_pending}"
            raise ValueError(msg)
        self._writer = writer
        self._clock = clock
        self._max_rows = max_rows
        self._interval_s = interval_s
        self._max_pending = max_pending
        self._pending: deque[CallEventRow] = deque()
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def pending(self) -> int:
        """Rows waiting to be written."""
        return len(self._pending)

    async def add(self, row: CallEventRow) -> None:
        """Queue a row; ``max_rows`` pending rows wake the flush loop; dropped after :meth:`aclose`."""
        if self._closed:
            _log.warning("events.call_events_dropped", count=1, reason="closed")
            return
        self._pending.append(row)
        self._trim()
        if len(self._pending) >= self._max_rows:
            self._wake.set()

    async def flush(self) -> int:
        """Write every pending row now; return the count. A writer error re-queues unwritten rows, raises."""
        async with self._lock:
            if not self._pending:
                return 0
            batch = list(self._pending)
            self._pending.clear()
            try:
                await self._writer.write(batch)
            except CallEventWriteError as exc:
                self._requeue(exc.unwritten)
                raise
            except BaseException:
                self._requeue(batch)
                raise
            return len(batch)

    async def flush_call(self, call_id: uuid.UUID) -> int:
        """Call end: flush now (everything pending, so the ended call's rows are written with the rest)."""
        written = await self.flush()
        _log.debug("events.call_events_flushed", call_id=str(call_id), rows=written)
        return written

    async def run(self) -> None:
        """Flush on every trigger (row threshold or interval) until :meth:`aclose`; errors are logged."""
        while True:
            await self._next_trigger()
            if self._closed:
                return
            await self._flush_logged()

    async def aclose(self) -> None:
        """Stop :meth:`run` and write what is pending (a failure is logged and the rows are kept)."""
        self._closed = True
        self._wake.set()
        await self._flush_logged()

    async def _flush_logged(self) -> None:
        try:
            await self.flush()
        except Exception as exc:  # the loop must survive any writer failure; the rows are retried
            _log.warning("events.call_events_flush_failed", error=type(exc).__name__, pending=self.pending)

    async def _next_trigger(self) -> None:
        self._wake.clear()
        if len(self._pending) >= self._max_rows or self._closed:
            return
        sleeper = asyncio.ensure_future(self._clock.sleep(self._interval_s))
        woken = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({sleeper, woken}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            woken.cancel()
            await asyncio.gather(sleeper, woken, return_exceptions=True)

    def _requeue(self, rows: Sequence[CallEventRow]) -> None:
        self._pending.extendleft(reversed(rows))
        self._trim()

    def _trim(self) -> None:
        excess = len(self._pending) - self._max_pending
        if excess <= 0:
            return
        for _ in range(excess):
            self._pending.popleft()
        _log.warning("events.call_events_dropped", count=excess, reason="overflow")
