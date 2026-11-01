"""Deadlines on the injected clock (Decision P03-T13a rule 11)."""

from __future__ import annotations

import asyncio

import pytest
from clock_helpers import ManualClock

from va_llm.deadline import DeadlineExceeded, within


async def test_result_before_the_deadline_cancels_the_timer() -> None:
    clock = ManualClock()

    async def work() -> str:
        await asyncio.sleep(0)
        return "done"

    assert await within(clock, 1.5, work()) == "done"
    assert clock.sleepers == 0


async def test_deadline_cancels_the_work() -> None:
    clock = ManualClock()
    cancelled = asyncio.Event()

    async def work() -> str:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "never"

    task = asyncio.create_task(within(clock, 1.5, work()))
    await clock.wait_for_sleep(1.5)
    await clock.run_for(1.49)
    assert not task.done()
    await clock.run_for(0.01)
    with pytest.raises(DeadlineExceeded):
        await task
    assert cancelled.is_set()


async def test_errors_of_the_work_propagate() -> None:
    clock = ManualClock()

    async def work() -> str:
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        await within(clock, 1.0, work())
    assert clock.sleepers == 0


async def test_a_result_that_lands_while_cancelling_still_wins() -> None:
    clock = ManualClock()

    async def work() -> str:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return "late"
        return "never"

    task = asyncio.create_task(within(clock, 1.0, work()))
    await clock.wait_for_sleep(1.0)
    await clock.run_for(1.0)
    assert await task == "late"


async def test_cancelling_the_caller_cancels_work_and_timer() -> None:
    clock = ManualClock()
    cancelled = asyncio.Event()

    async def work() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(within(clock, 5.0, work()))
    await clock.wait_for_sleep(5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert clock.sleepers == 0
