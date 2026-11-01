"""Deadlines on the injected clock (Decision P03-T13a rule 11): tests with a manual clock decide exactly
when a request times out, and production uses the event loop's timer through
:class:`va_common.time.SystemClock`."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from va_common.time import Clock

__all__ = ["DeadlineExceeded", "within"]


class DeadlineExceeded(Exception):  # noqa: N818 - mirrors the builtin TimeoutError naming without shadowing it
    """``work`` did not finish within the deadline; it has been cancelled."""


async def _cancel(*tasks: asyncio.Future[Any]) -> None:
    pending = [task for task in tasks if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending)


async def within[T](clock: Clock, seconds: float, work: Coroutine[Any, Any, T]) -> T:
    """Run ``work``; cancel it and raise :class:`DeadlineExceeded` when ``clock`` passes ``seconds`` first.

    A result that lands while ``work`` is being cancelled still wins; cancelling the caller cancels both.
    """
    task = asyncio.ensure_future(work)
    timer = asyncio.ensure_future(clock.sleep(seconds))
    try:
        await asyncio.wait((task, timer), return_when=asyncio.FIRST_COMPLETED)
        if not task.done():
            await _cancel(task)
        if task.cancelled():
            raise DeadlineExceeded
        return task.result()
    finally:
        await _cancel(task, timer)
