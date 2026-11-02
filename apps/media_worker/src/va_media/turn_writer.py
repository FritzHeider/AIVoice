"""The media worker's write-behind ``call_turns`` writer (specs/07-media-pipeline.md §8 steps 2 and 6;
specs/07 §8 Decision P05-T11a).

The pipeline's observer queues :data:`~va_db.call_turns.TurnOp` items with :meth:`TurnWriter.add` (sync, so
a pipeline callback never awaits). Every 2 s (on the injected clock), at call end
(:meth:`TurnWriter.flush_call`) and on :meth:`TurnWriter.aclose` the pending ops are applied in queue order,
one tenant transaction per org (consecutive rows of an org go out as one upsert). Flushes are serialised. A
failed org's ops go back to the front of the queue in their order (the others stay written) and the next
trigger retries them; every op is idempotent, so a re-applied op is harmless. At most ``max_pending`` ops are
kept; beyond that the oldest are dropped with a ``media.turns_dropped`` warning (a count, never turn text).
Run :meth:`TurnWriter.run` under the owner's supervisor.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from collections.abc import Sequence
from typing import Final

from va_common.logging import get_logger
from va_common.time import Clock
from va_db.call_turns import CallTurnRow, LatencyMerge, TurnDelete, TurnOp
from va_db.repositories import CallTurnRepo
from va_db.session import Database
from va_events.batching import wait_for_trigger

__all__ = ["DEFAULT_MAX_PENDING_OPS", "TURN_FLUSH_INTERVAL_S", "TurnWriteError", "TurnWriter"]

TURN_FLUSH_INTERVAL_S: Final = 2.0
DEFAULT_MAX_PENDING_OPS: Final = 10_000

_log = get_logger(__name__)


class TurnWriteError(Exception):
    """Some orgs' ops were not written (they are queued again); the first failure is chained."""

    def __init__(self, unwritten: int, *, failed_orgs: int) -> None:
        super().__init__(f"{unwritten} call_turns ops of {failed_orgs} org(s) were not written")
        self.unwritten = unwritten
        self.failed_orgs = failed_orgs


class TurnWriter:
    """Collects turn ops and applies them in batches."""

    def __init__(
        self,
        database: Database,
        *,
        clock: Clock,
        interval_s: float = TURN_FLUSH_INTERVAL_S,
        max_pending: int = DEFAULT_MAX_PENDING_OPS,
    ) -> None:
        if max_pending <= 0:
            msg = f"max_pending must be positive, got {max_pending}"
            raise ValueError(msg)
        self._database = database
        self._clock = clock
        self._interval_s = interval_s
        self._max_pending = max_pending
        self._pending: deque[TurnOp] = deque()
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def pending(self) -> int:
        """Ops waiting to be written."""
        return len(self._pending)

    def add(self, op: TurnOp) -> None:
        """Queue an op (dropped with a warning after :meth:`aclose`)."""
        if self._closed:
            _log.warning("media.turns_dropped", count=1, reason="closed")
            return
        self._pending.append(op)
        self._trim()

    async def flush(self) -> int:
        """Apply every pending op now; returns how many were written. Raises :class:`TurnWriteError` after
        re-queueing the ops of the orgs that failed."""
        return await self._flush(None)

    async def flush_call(self, call_id: uuid.UUID) -> int:
        """Call end: apply the call's pending ops now (other calls wait for the next trigger)."""
        written = await self._flush(call_id)
        _log.debug("media.turns_flushed", call_id=str(call_id), ops=written)
        return written

    async def run(self) -> None:
        """Flush every interval until :meth:`aclose`; failures are logged and retried."""
        while await self._next_trigger():
            await self._flush_logged()

    async def _next_trigger(self) -> bool:
        """Wait for the interval (or :meth:`aclose`); ``False`` once closed."""
        self._wake.clear()
        if not self._closed:
            await wait_for_trigger(self._clock, self._wake, self._interval_s)
        return not self._closed

    async def aclose(self) -> None:
        """Stop :meth:`run` and write what is pending (a failure is logged; the ops are kept)."""
        self._closed = True
        self._wake.set()
        await self._flush_logged()

    async def _flush_logged(self) -> None:
        try:
            await self.flush()
        except Exception as exc:  # the loop must survive any database failure; the ops are retried
            _log.warning("media.turns_flush_failed", error=type(exc).__name__, pending=self.pending)

    async def _flush(self, call_id: uuid.UUID | None) -> int:
        async with self._lock:
            batch = self._take(call_id)
            if not batch:
                return 0
            by_org: dict[uuid.UUID, list[TurnOp]] = {}
            for op in batch:
                by_org.setdefault(op.org_id, []).append(op)
            failed: set[uuid.UUID] = set()
            first_error: Exception | None = None
            for org_id, ops in by_org.items():
                try:
                    await self._apply(org_id, ops)
                except Exception as exc:  # every org is attempted; the failed ones are re-queued below
                    failed.add(org_id)
                    first_error = first_error or exc
                    _log.warning(
                        "media.turns_org_write_failed",
                        org_id=str(org_id),
                        ops=len(ops),
                        error=type(exc).__name__,
                    )
            if not failed:
                return len(batch)
            unwritten = [op for op in batch if op.org_id in failed]
            self._pending.extendleft(reversed(unwritten))
            self._trim()
            raise TurnWriteError(len(unwritten), failed_orgs=len(failed)) from first_error

    def _take(self, call_id: uuid.UUID | None) -> list[TurnOp]:
        if call_id is None:
            batch = list(self._pending)
            self._pending.clear()
            return batch
        batch = [op for op in self._pending if op.call_id == call_id]
        if batch:
            kept = [op for op in self._pending if op.call_id != call_id]
            self._pending = deque(kept)
        return batch

    async def _apply(self, org_id: uuid.UUID, ops: Sequence[TurnOp]) -> None:
        async with self._database.tenant_session(org_id) as session:
            repo = CallTurnRepo(session, org_id)
            rows: list[CallTurnRow] = []
            for op in ops:
                if isinstance(op, CallTurnRow):
                    rows.append(op)
                    continue
                await repo.upsert_many(rows)
                rows = []
                await _apply_change(repo, op)
            await repo.upsert_many(rows)

    def _trim(self) -> None:
        excess = len(self._pending) - self._max_pending
        if excess <= 0:
            return
        for _ in range(excess):
            self._pending.popleft()
        _log.warning("media.turns_dropped", count=excess, reason="overflow")


async def _apply_change(repo: CallTurnRepo, op: LatencyMerge | TurnDelete) -> None:
    if isinstance(op, LatencyMerge):
        found = await repo.merge_latency(op.call_id, op.turn_index, op.role, op.latency)
    else:
        found = await repo.delete_turn(op.call_id, op.turn_index, op.role)
    if not found:
        _log.debug("media.turn_op_without_row", op=type(op).__name__, turn_index=op.turn_index)
