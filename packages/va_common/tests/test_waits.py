"""Clock-driven waits (``va_common.waits``)."""

from __future__ import annotations

import asyncio

import pytest
from clock_helpers import ManualClock

from va_common.waits import wait_until_set, within


async def test_within_returns_true_when_the_work_finishes_first() -> None:
    clock = ManualClock()

    async def work() -> None:
        await asyncio.sleep(0)

    assert await within(work(), clock, 1.0)


async def test_within_cancels_the_work_when_the_clock_runs_out() -> None:
    clock = ManualClock()
    cancelled = asyncio.Event()

    async def hang() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    waiting = asyncio.ensure_future(within(hang(), clock, 2.0))
    await clock.wait_for_sleep(2.0)
    await clock.run_for(2.0)
    assert await waiting is False
    assert cancelled.is_set()


async def test_within_without_a_timeout_awaits_the_work() -> None:
    async def value() -> int:
        return 1

    assert await within(value(), ManualClock(), None)


async def test_within_propagates_the_work_error() -> None:
    async def fail() -> None:
        raise LookupError

    with pytest.raises(LookupError):
        await within(fail(), ManualClock(), 1.0)


async def test_wait_until_set_returns_at_once_for_a_set_event() -> None:
    event = asyncio.Event()
    event.set()
    assert await wait_until_set(event, ManualClock(), 0.0)


async def test_wait_until_set_times_out_on_the_clock() -> None:
    clock = ManualClock()
    waiting = asyncio.ensure_future(wait_until_set(asyncio.Event(), clock, 1.0))
    await clock.wait_for_sleep(1.0)
    await clock.run_for(1.0)
    assert await waiting is False
