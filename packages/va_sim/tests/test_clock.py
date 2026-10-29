"""Simulator clocks: realtime follows the wall clock, accelerated runs on ordered virtual time."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from va_sim.clock import DEFAULT_START, AcceleratedClock, RealtimeClock, clock_for


def test_realtime_clock_reads_wall_and_monotonic_time() -> None:
    readings = iter([100.0, 100.25, 101.0])
    clock = RealtimeClock(monotonic=lambda: next(readings))
    assert clock.mode == "realtime"
    assert clock.virtual_ms() == 250
    assert clock.monotonic() == 101.0
    now = clock.now()
    assert now.tzinfo is UTC
    assert abs(now - datetime.now(UTC)) < timedelta(seconds=5)


async def test_realtime_clock_sleeps_on_the_event_loop() -> None:
    clock = RealtimeClock()
    await clock.sleep(0)
    with pytest.raises(ValueError, match="negative"):
        await clock.sleep(-0.1)


async def test_accelerated_sleep_advances_virtual_time_without_waiting() -> None:
    loop = asyncio.get_running_loop()
    clock = AcceleratedClock()
    started = loop.time()
    await clock.sleep(1.5)
    assert loop.time() - started < 0.5
    assert clock.mode == "accelerated"
    assert clock.virtual_ms() == 1500
    assert clock.monotonic() == 1.5
    assert clock.now() == DEFAULT_START + timedelta(seconds=1.5)


async def test_concurrent_sleepers_wake_in_deadline_order() -> None:
    clock = AcceleratedClock(start=datetime(2026, 3, 1, tzinfo=UTC))
    woke: list[tuple[str, int]] = []

    async def sleeper(name: str, seconds: float) -> None:
        await clock.sleep(seconds)
        woke.append((name, clock.virtual_ms()))

    async with asyncio.TaskGroup() as group:
        group.create_task(sleeper("slow", 0.3))
        group.create_task(sleeper("fast", 0.1))
    assert woke == [("fast", 100), ("slow", 300)]


async def test_a_sleeper_that_sleeps_again_keeps_its_place_in_virtual_time() -> None:
    clock = AcceleratedClock()
    woke: list[tuple[str, int]] = []

    async def ticker() -> None:
        for _ in range(3):
            await clock.sleep(0.1)
            woke.append(("tick", clock.virtual_ms()))

    async def late() -> None:
        await clock.sleep(0.25)
        woke.append(("late", clock.virtual_ms()))

    async with asyncio.TaskGroup() as group:
        group.create_task(ticker())
        group.create_task(late())
    assert woke == [("tick", 100), ("tick", 200), ("late", 250), ("tick", 300)]


async def test_cancelled_sleeper_does_not_move_time() -> None:
    clock = AcceleratedClock()
    doomed = asyncio.ensure_future(clock.sleep(5))
    await asyncio.sleep(0)
    doomed.cancel()
    await clock.sleep(0.2)
    assert clock.virtual_ms() == 200


async def test_advance_wakes_due_sleepers() -> None:
    clock = AcceleratedClock()
    clock.advance(2)
    assert clock.virtual_ms() == 2000
    waiting = asyncio.ensure_future(clock.sleep(1))
    await asyncio.sleep(0)
    clock.advance(1)
    await waiting
    assert clock.virtual_ms() == 3000


async def test_negative_durations_are_rejected() -> None:
    clock = AcceleratedClock()
    with pytest.raises(ValueError, match="negative"):
        await clock.sleep(-1)
    with pytest.raises(ValueError, match="negative"):
        clock.advance(-1)


def test_naive_start_is_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        AcceleratedClock(start=datetime(2026, 1, 1))  # noqa: DTZ001 - the naive value under test


def test_realtime_clock_defaults_to_the_process_monotonic_clock() -> None:
    clock = RealtimeClock()
    assert clock.virtual_ms() >= 0
    assert clock.monotonic() > 0


def test_clock_for_mode() -> None:
    assert isinstance(clock_for("accelerated"), AcceleratedClock)
    assert isinstance(clock_for("realtime"), RealtimeClock)
