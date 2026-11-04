"""``va_elevenlabs.ws_tasks.stop_tasks``: stopped tasks leave the owner's list (specs/07 §9 Decision
P08-FIX-3)."""

from __future__ import annotations

import asyncio

from va_elevenlabs.ws_tasks import stop_tasks


async def _forever() -> None:
    await asyncio.Event().wait()


async def test_stop_tasks_cancels_awaits_and_forgets_the_tasks() -> None:
    tasks = [asyncio.create_task(_forever()), asyncio.create_task(_forever())]
    stopped = list(tasks)
    await asyncio.sleep(0)

    await stop_tasks(tasks)

    assert tasks == []
    assert all(task.cancelled() for task in stopped)


async def test_stop_tasks_keeps_the_calling_task() -> None:
    other = asyncio.create_task(_forever())
    tasks: list[asyncio.Task[None]] = [other]

    left: list[asyncio.Task[None]] = []

    async def stopper() -> None:
        await stop_tasks(tasks)
        left.extend(tasks)

    own = asyncio.create_task(stopper())
    tasks.append(own)
    await own

    assert left == [own]
    assert other.cancelled()
