"""Task helpers shared by the TTS and STT socket clients: the timer nap, crash detection, and stopping."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from va_common.time import Clock

__all__ = ["crash_of", "nap", "run_timer", "stop_tasks"]


async def nap(clock: Clock, wake: asyncio.Event, delay: float | None) -> None:
    """Sleep on ``clock`` for ``delay`` seconds (forever when ``None``) or until ``wake`` is set."""
    pending: set[asyncio.Task[object]] = {asyncio.create_task(wake.wait())}
    if delay is not None:
        pending.add(asyncio.create_task(clock.sleep(max(delay, 0.0))))
    try:
        await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def run_timer(
    clock: Clock,
    wake: asyncio.Event,
    run_due: Callable[[], Awaitable[float | None]],
    running: Callable[[], bool],
) -> None:
    """While ``running()``: run the due actions, then nap until the next deadline ``run_due`` returned (in
    seconds from now; ``None``: none) or until ``wake`` is set."""
    while running():
        wake.clear()
        delay = await run_due()
        await nap(clock, wake, delay)


def crash_of(task: asyncio.Task[None]) -> BaseException | None:
    """The exception a finished ``task`` died of (``None`` when it returned or was cancelled)."""
    if task.cancelled():
        return None
    return task.exception()


async def stop_tasks(tasks: Sequence[asyncio.Task[None]]) -> None:
    """Cancel and await ``tasks`` except the calling task."""
    current = asyncio.current_task()
    others = [task for task in tasks if task is not current]
    for task in others:
        task.cancel()
    await asyncio.gather(*others, return_exceptions=True)
