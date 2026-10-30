"""Supervisor: failures are logged not raised, cancellation, drain follows spawned tasks."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from va_sim import tasks as tasks_module
from va_sim.tasks import Supervisor


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
