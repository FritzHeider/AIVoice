"""``va_common.gc_tuning``: the collector tuning and the pause log (specs/07 §9 Decisions P08-FIX-3/4)."""

from __future__ import annotations

import asyncio
import gc
import io

import pytest
import structlog
from service_helpers import json_log_stream, log_events

from va_common import gc_tuning
from va_common.gc_tuning import GC_PAUSE_THRESHOLD_MS, GcPauseLog, installed_pause_log, tune_gc
from va_common.logging import bind_call_context


class StepClock:
    """Each read returns the next scripted time (seconds)."""

    def __init__(self, *times: float) -> None:
        self._times = list(times)

    def __call__(self) -> float:
        return self._times.pop(0)


def _events(stream: io.StringIO, name: str) -> list[dict[str, object]]:
    return [e for e in log_events(stream) if e["event"] == name]


def _pause(log: GcPauseLog, generation: int = 1, collected: int = 42) -> None:
    log._callback("start", {"generation": generation})
    log._callback("stop", {"generation": generation, "collected": collected, "uncollectable": 0})


def test_a_collection_at_or_above_the_threshold_is_logged_at_the_flush() -> None:
    stream = json_log_stream()
    log = GcPauseLog(clock=StepClock(2.0, 2.25))
    _pause(log)
    assert _events(stream, "gc_pause") == []
    assert log.pauses == 1
    assert log.flush() == 1
    [paused] = _events(stream, "gc_pause")
    assert paused["level"] == "warning"
    assert (paused["generation"], paused["collected"], paused["uncollectable"]) == (1, 42, 0)
    assert paused["duration_ms"] == 250.0
    assert log.flush() == 0


def test_a_short_collection_and_a_stop_without_start_are_not_logged() -> None:
    stream = json_log_stream()
    log = GcPauseLog(clock=StepClock(1.0, 1.0 + GC_PAUSE_THRESHOLD_MS / 2000))
    log._callback("stop", {"generation": 0, "collected": 0, "uncollectable": 0})
    log._callback("start", {"generation": 0})
    log._callback("stop", {"generation": 0, "collected": 0, "uncollectable": 0})
    assert log.flush() == 0
    assert _events(stream, "gc_pause") == []
    assert log.pauses == 0


def test_gc_pause_is_logged_outside_the_call_context() -> None:
    stream = json_log_stream()
    log = GcPauseLog(clock=StepClock(0.0, 0.1))
    try:
        bind_call_context(org_id="org-1", call_id="call-1")
        _pause(log)
        log.flush()
    finally:
        structlog.contextvars.clear_contextvars()
    [paused] = _events(stream, "gc_pause")
    assert "call_id" not in paused
    assert "org_id" not in paused


def test_overflow_keeps_the_latest_and_reports_the_dropped() -> None:
    stream = json_log_stream()
    log = GcPauseLog(clock=StepClock(0.0, 0.1, 1.0, 1.1, 2.0, 2.1), capacity=2)
    for collected in (1, 2, 3):
        _pause(log, collected=collected)
    assert log.flush() == 2
    assert [e["collected"] for e in _events(stream, "gc_pause")] == [2, 3]
    [overflow] = _events(stream, "gc_pause_overflow")
    assert overflow["dropped"] == 1
    assert log.dropped == 0
    log.flush()
    assert len(_events(stream, "gc_pause_overflow")) == 1


async def test_run_flushes_each_interval_and_on_cancel() -> None:
    stream = json_log_stream()
    log = GcPauseLog(clock=StepClock(0.0, 0.1, 1.0, 1.1))
    gates: asyncio.Queue[asyncio.Event] = asyncio.Queue()
    intervals: list[float] = []

    async def sleep(seconds: float) -> None:
        intervals.append(seconds)
        gate = asyncio.Event()
        await gates.put(gate)
        await gate.wait()

    task = asyncio.create_task(log.run(interval_s=0.5, sleep=sleep))
    first = await gates.get()
    _pause(log, collected=1)
    first.set()
    await gates.get()
    assert [e["collected"] for e in _events(stream, "gc_pause")] == [1]
    _pause(log, collected=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [e["collected"] for e in _events(stream, "gc_pause")] == [1, 2]
    assert intervals == [0.5, 0.5]


def test_install_and_uninstall_are_idempotent() -> None:
    log = GcPauseLog(threshold_ms=0.0)
    try:
        log.install()
        log.install()
        assert gc.callbacks.count(log._callback) == 1
        gc.collect(0)
        assert log.pauses >= 1
    finally:
        log.uninstall()
        log.uninstall()
    assert log._callback not in gc.callbacks


def test_tune_gc_freezes_sets_the_thresholds_and_installs_the_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gc_tuning, "_installed", None)
    thresholds = gc.get_threshold()
    pauses = None
    try:
        pauses = tune_gc((12_345, 6, 70))
        assert gc.get_threshold() == (12_345, 6, 70)
        assert gc.get_freeze_count() > 0
        assert pauses._callback in gc.callbacks
        assert installed_pause_log() is pauses
    finally:
        if pauses is not None:
            pauses.uninstall()
        gc.unfreeze()
        gc.set_threshold(*thresholds)
