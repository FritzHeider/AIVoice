"""Supervisor: failures are logged not raised, cancellation, drain follows spawned tasks; hold_clock."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from va_sim import tasks as tasks_module
from va_sim.clock import AcceleratedClock, RealtimeClock
from va_sim.tasks import Supervisor, hold_clock


class _RecordingLogger:
    def __init__(self) -> None:
        self.errors: list[tuple[str, dict[str, Any]]] = []

    def error(self, event: str, **fields: Any) -> None:
        self.errors.append((event, fields))


async def test_failing_task_is_logged_and_forgotten(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = _RecordingLogger()
    monkeypatch.setattr(tasks_module, "_log", logger)
    supervisor = Supervisor()

    async def boom() -> None:
        msg = "kaput"
        raise RuntimeError(msg)

    task = supervisor.spawn(boom(), name="boom")
    await supervisor.drain()
    assert task.done()
    assert supervisor.pending == 0
    [(event, fields)] = logger.errors
    assert event == "sim.task_failed"
    assert fields["task"] == "boom"
    assert "kaput" in fields["error"]
    assert isinstance(fields["exc_info"], RuntimeError)


async def test_cancel_all_and_aclose_stop_pending_tasks() -> None:
    supervisor = Supervisor()
    gate = asyncio.Event()
    first = supervisor.spawn(_wait(gate), name="first")
    second = supervisor.spawn(_wait(gate), name="second")
    await asyncio.sleep(0)
    assert supervisor.pending == 2
    supervisor.cancel_all()
    await asyncio.wait({first, second})
    await asyncio.sleep(0)
    assert first.cancelled()
    assert supervisor.pending == 0
    assert second.cancelled()
    third = supervisor.spawn(_wait(gate), name="third")
    await supervisor.aclose()
    assert third.cancelled()
    assert supervisor.pending == 0


async def _wait(gate: asyncio.Event) -> None:
    await gate.wait()


async def test_drain_waits_for_tasks_spawned_by_tasks() -> None:
    supervisor = Supervisor()
    order: list[str] = []

    async def child() -> None:
        await asyncio.sleep(0)
        order.append("child")

    async def parent() -> None:
        order.append("parent")
        supervisor.spawn(child(), name="child")

    supervisor.spawn(parent(), name="parent")
    await supervisor.drain()
    assert order == ["parent", "child"]
    assert supervisor.pending == 0


def test_spawn_needs_a_running_loop() -> None:
    supervisor = Supervisor()
    coro = _wait(asyncio.Event())
    with pytest.raises(RuntimeError):
        supervisor.spawn(coro, name="no-loop")
    coro.close()


async def test_hold_clock_freezes_accelerated_time_while_a_thread_runs() -> None:
    clock = AcceleratedClock()
    supervisor = Supervisor()
    woke: list[int] = []

    async def sleeper() -> None:
        await clock.sleep(1.0)
        woke.append(clock.virtual_ms())

    supervisor.spawn(sleeper(), name="sleeper")
    await asyncio.sleep(0)
    async with hold_clock(clock, supervisor, name="hold"):
        await asyncio.to_thread(time.sleep, 0.05)
        assert clock.virtual_ms() == 0
        assert woke == []
    await clock.sleep(2.0)
    assert woke == [1000]
    await supervisor.drain()
    assert supervisor.pending == 0


async def test_unheld_accelerated_clock_jumps_during_a_thread() -> None:
    clock = AcceleratedClock()
    supervisor = Supervisor()

    async def sleeper() -> None:
        await clock.sleep(1.0)

    task = supervisor.spawn(sleeper(), name="sleeper")
    await asyncio.sleep(0)
    await asyncio.to_thread(time.sleep, 0.05)
    await asyncio.wait({task})
    assert clock.virtual_ms() == 1000


async def test_hold_clock_is_a_no_op_on_a_realtime_clock() -> None:
    supervisor = Supervisor()
    async with hold_clock(RealtimeClock(), supervisor, name="hold"):
        assert supervisor.pending == 0
