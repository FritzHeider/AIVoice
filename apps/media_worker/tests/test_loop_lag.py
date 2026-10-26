"""Event loop lag liveness (specs/27-observability.md §9: media liveness requires lag < 500 ms)."""

from __future__ import annotations

import asyncio

from va_common.metrics import Service, build_metrics, new_registry
from va_media.loop_lag import LAG_REASON, LoopLagMonitor


def test_record_threshold_and_histogram() -> None:
    metrics = build_metrics(Service.MEDIA_WORKER, new_registry(process_collectors=False))
    monitor = LoopLagMonitor(histogram=metrics.histogram("va_event_loop_lag_ms"))
    assert monitor.liveness() is None
    monitor.record(0.6)
    assert (monitor.healthy, monitor.liveness()) == (False, LAG_REASON)
    monitor.record(0.01)
    assert monitor.liveness() is None
    total = metrics.registry.get_sample_value("va_event_loop_lag_ms_sum")
    assert total is not None
    assert round(total) == 610


async def test_run_produces_a_healthy_first_sample() -> None:
    monitor = LoopLagMonitor(interval_s=0.001)
    task = asyncio.ensure_future(monitor.run())
    try:
        await asyncio.wait_for(monitor.first_sample.wait(), timeout=5)
        assert monitor.lag_s < monitor.threshold_s
        assert monitor.liveness() is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
