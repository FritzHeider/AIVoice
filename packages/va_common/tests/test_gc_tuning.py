"""``va_common.gc_tuning``: the collector tuning and the pause log (specs/07 §9 Decision P08-FIX-3)."""

from __future__ import annotations

import gc

from service_helpers import json_log_stream, log_events

from va_common.gc_tuning import GC_PAUSE_THRESHOLD_MS, GcPauseLog, tune_gc


class StepClock:
    """Each read returns the next scripted time (seconds)."""

    def __init__(self, *times: float) -> None:
        self._times = list(times)

    def __call__(self) -> float:
        return self._times.pop(0)


def _pauses(stream: object) -> list[dict[str, object]]:
    return [e for e in log_events(stream) if e["event"] == "gc_pause"]  # type: ignore[arg-type]


def test_a_collection_at_or_above_the_threshold_is_logged() -> None:
    stream = json_log_stream()
    log = GcPauseLog(clock=StepClock(2.0, 2.25))
    log._callback("start", {"generation": 1})
    log._callback("stop", {"generation": 1, "collected": 42, "uncollectable": 0})
    [paused] = _pauses(stream)
    assert paused["level"] == "warning"
    assert (paused["generation"], paused["collected"], paused["uncollectable"]) == (1, 42, 0)
    assert paused["duration_ms"] == 250.0
    assert log.pauses == 1


def test_a_short_collection_and_a_stop_without_start_are_not_logged() -> None:
    stream = json_log_stream()
    log = GcPauseLog(clock=StepClock(1.0, 1.0 + GC_PAUSE_THRESHOLD_MS / 2000))
    log._callback("stop", {"generation": 0, "collected": 0, "uncollectable": 0})
    log._callback("start", {"generation": 0})
    log._callback("stop", {"generation": 0, "collected": 0, "uncollectable": 0})
    assert _pauses(stream) == []
    assert log.pauses == 0


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


def test_tune_gc_freezes_sets_the_thresholds_and_installs_the_log() -> None:
    thresholds = gc.get_threshold()
    pauses = None
    try:
        pauses = tune_gc((12_345, 6, 70))
        assert gc.get_threshold() == (12_345, 6, 70)
        assert gc.get_freeze_count() > 0
        assert pauses._callback in gc.callbacks
    finally:
        if pauses is not None:
            pauses.uninstall()
        gc.unfreeze()
        gc.set_threshold(*thresholds)
