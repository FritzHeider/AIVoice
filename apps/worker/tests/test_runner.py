"""Temporal worker lifecycle with fake workers (no Temporal server)."""

from __future__ import annotations

import asyncio

import pytest
from service_helpers import json_log_stream, log_events
from worker_helpers import Factory, FakeClientSource, FakeWorker

from va_worker.runner import STOPPED_REASON, TemporalWorkers
from va_worker.task_queues import TaskQueueRegistration

REGISTRATION = TaskQueueRegistration("va-main", workflows=[], activities=[])


async def test_no_registrations_returns_without_connecting() -> None:
    stream = json_log_stream()
    source = FakeClientSource()
    workers = TemporalWorkers(())
    await workers.run(source)
    await workers.shutdown()
    assert source.connects == 0
    assert workers.liveness() is None
    assert any(e["event"] == "worker.no_task_queues" for e in log_events(stream))


async def test_workers_run_until_shutdown_then_liveness_reports_stopped() -> None:
    worker = FakeWorker()
    factory = Factory(worker)
    workers = TemporalWorkers([REGISTRATION], worker_factory=factory)
    running = asyncio.ensure_future(workers.run(FakeClientSource()))
    await asyncio.wait_for(worker.started.wait(), timeout=5)
    assert [queue for _, queue in factory.built] == ["va-main"]
    assert workers.liveness() is None
    await workers.shutdown()
    await asyncio.wait_for(running, timeout=5)
    assert worker.shutdowns == 1
    assert workers.liveness() == STOPPED_REASON


async def test_one_worker_per_registration_and_failure_propagates() -> None:
    healthy, broken = FakeWorker(), FakeWorker(RuntimeError("poller died"))
    registrations = [REGISTRATION, TaskQueueRegistration("va-postcall", workflows=[], activities=[])]
    workers = TemporalWorkers(registrations, worker_factory=Factory(healthy, broken))
    with pytest.raises(ExceptionGroup) as caught:
        await workers.run(FakeClientSource())
    assert caught.group_contains(RuntimeError, match="poller died")
    assert workers.liveness() == STOPPED_REASON
