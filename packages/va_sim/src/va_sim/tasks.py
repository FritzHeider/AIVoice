"""Supervised background tasks for the simulators (MASTER_PROMPT §4.7: every background task is supervised).

Simulators schedule delayed work (number order completion, message lifecycle events, webhook retries) as tasks
owned by one :class:`Supervisor`. A failing task is logged, never re-raised into the request that spawned it;
``/sim/reset`` cancels everything pending and shutdown awaits the cancellation. :func:`hold_clock` keeps an
accelerated clock at the current instant while CPU work runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Coroutine
from typing import TYPE_CHECKING, Any, Final

from va_common.logging import get_logger

if TYPE_CHECKING:
    from va_sim.clock import SimClock

__all__ = ["Supervisor", "hold_clock"]

_log: Final = get_logger(__name__)


class Supervisor:
    """Owns the simulator's background tasks."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def pending(self) -> int:
        """Tasks that have not finished yet."""
        return len(self._tasks)

    def spawn(self, coro: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]:
        """Run ``coro`` in the background under this supervisor."""
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _log.error("sim.task_failed", task=task.get_name(), error=repr(error), exc_info=error)

    def cancel_all(self) -> None:
        """Request cancellation of every pending task (synchronous, for ``/sim/reset``)."""
        for task in tuple(self._tasks):
            task.cancel()

    async def aclose(self) -> None:
        """Cancel every pending task and wait until they have all finished."""
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def drain(self) -> None:
        """Wait until no task is pending, including tasks spawned while waiting (tests)."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
            # gather returns without yielding when every task is already done; give their done callbacks
            # (which remove them from the set) a loop turn so this cannot spin.
            await asyncio.sleep(0)


@contextlib.asynccontextmanager
async def hold_clock(clock: SimClock, tasks: Supervisor, *, name: str) -> AsyncIterator[None]:
    """Keep an accelerated ``clock`` at the current instant inside the block (no-op on a realtime clock).

    Virtual time moves only while the event loop is idle, and awaiting real I/O or a worker thread leaves it
    idle; a holder task sleeping zero seconds in a loop is always the earliest sleeper, so the clock never
    jumps past it until the block exits.
    """
    if clock.mode != "accelerated":
        yield
        return

    async def hold() -> None:
        while True:
            await clock.sleep(0)

    holder = tasks.spawn(hold(), name=name)
    try:
        yield
    finally:
        holder.cancel()
