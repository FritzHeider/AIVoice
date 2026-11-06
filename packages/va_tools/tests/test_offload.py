"""The one reader thread (specs/13 §9 Decision P08-FIX-25)."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading

import pytest
from service_helpers import json_log_stream, log_events
from tools_helpers import HOLD_CAP_S, held_reader

from va_common.logging import bound_context, get_logger
from va_tools.offload import READER_THREAD, off_loop

JOBS = 5
_log = get_logger(__name__)
_tenant: contextvars.ContextVar[str] = contextvars.ContextVar("tenant", default="unset")


class _Gated:
    """Jobs that each wait for their own gate and record where and when they ran."""

    def __init__(self, loop: asyncio.AbstractEventLoop, count: int) -> None:
        self._loop = loop
        self._lock = threading.Lock()
        self._inside = 0
        self.peak = 0
        self.order: list[int] = []
        self.threads: set[str] = set()
        self.gates = [threading.Event() for _ in range(count)]
        self.started = [asyncio.Event() for _ in range(count)]

    def job(self, number: int) -> int:
        with self._lock:
            self._inside += 1
            self.peak = max(self.peak, self._inside)
            self.order.append(number)
            self.threads.add(threading.current_thread().name)
        self._loop.call_soon_threadsafe(self.started[number].set)
        self.gates[number].wait(HOLD_CAP_S)
        with self._lock:
            self._inside -= 1
        return number


async def test_work_runs_on_the_one_reader_thread_in_order() -> None:
    jobs = _Gated(asyncio.get_running_loop(), JOBS)
    tasks = [asyncio.ensure_future(off_loop(functools.partial(jobs.job, number))) for number in range(JOBS)]
    try:
        for number in range(JOBS):
            async with asyncio.timeout(HOLD_CAP_S):
                await jobs.started[number].wait()
            assert jobs.order == list(range(number + 1))
            jobs.gates[number].set()
        results = await asyncio.gather(*tasks)
    finally:
        for gate in jobs.gates:
            gate.set()

    assert results == list(range(JOBS))
    assert jobs.peak == 1
    (thread,) = jobs.threads
    assert thread.startswith(READER_THREAD)
    assert thread != threading.current_thread().name


async def test_the_caller_s_context_variables_reach_the_thread() -> None:
    stream = json_log_stream()
    token = _tenant.set("acme")
    try:
        with bound_context(call_id="call-7"):
            seen = await off_loop(_tenant.get)
            await off_loop(functools.partial(_log.info, "offload.probe"))
    finally:
        _tenant.reset(token)

    assert seen == "acme"
    (line,) = [event for event in log_events(stream) if event["event"] == "offload.probe"]
    assert line["call_id"] == "call-7"


async def test_an_exception_of_the_work_propagates() -> None:
    with pytest.raises(ValueError, match="invalid literal"):
        await off_loop(functools.partial(int, "not a number"))


async def test_a_cancelled_wait_returns_while_the_thread_is_still_held() -> None:
    """The job that is running finishes on its own; the task that awaited it does not wait for that."""
    jobs = _Gated(asyncio.get_running_loop(), 1)
    waiting = asyncio.ensure_future(off_loop(functools.partial(jobs.job, 0)))
    try:
        async with asyncio.timeout(HOLD_CAP_S):
            await jobs.started[0].wait()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        still_held = not jobs.gates[0].is_set() and jobs.peak == 1
    finally:
        jobs.gates[0].set()

    assert still_held
    assert await off_loop(functools.partial(str, "after")) == "after"


async def test_a_job_cancelled_while_queued_never_runs() -> None:
    ran: list[str] = []
    async with held_reader() as gate:
        queued = asyncio.ensure_future(off_loop(functools.partial(ran.append, "queued")))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert not gate.is_set()

    await off_loop(functools.partial(ran.append, "marker"))
    assert ran == ["marker"]
