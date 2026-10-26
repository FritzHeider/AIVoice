"""Worker probe app (specs/27-observability.md §9) and entry point."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from service_helpers import (
    UNREACHABLE,
    FakeContainer,
    capture_serve,
    make_client,
    ok_check,
    readiness_of,
    settings_of,
)
from worker_helpers import Factory, FakeClientSource, FakeWorker

from va_common.service import Bind
from va_worker import __version__
from va_worker import main as worker_main
from va_worker.main import build_container, create_app
from va_worker.settings import WorkerSettings
from va_worker.task_queues import TaskQueueRegistration

ALL_OK = {"postgres": ok_check, "redis": ok_check, "temporal": ok_check}


@dataclass
class FakeWorkerContainer(FakeContainer):
    checks: Any = field(default_factory=lambda: dict(ALL_OK))
    temporal: FakeClientSource = field(default_factory=FakeClientSource)


def worker_app(container: FakeWorkerContainer | None = None, **kwargs: Any) -> Any:
    return create_app(
        settings_of(WorkerSettings),
        container_factory=container if container is not None else FakeWorkerContainer(),
        init_observability=False,
        **kwargs,
    )


async def test_probes_without_task_queues() -> None:
    async with make_client(worker_app()) as client:
        live, ready = await client.get("/healthz"), await client.get("/readyz")
        metrics = await client.get("/metrics")
    assert live.json() == {"status": "ok", "version": __version__}
    assert ready.json()["checks"] == {"postgres": "ok", "redis": "ok", "temporal": "ok"}
    assert "# TYPE va_workflow_started_total counter" in metrics.text


async def test_readyz_real_container_reports_unreachable_dependencies() -> None:
    status, body = await readiness_of(
        create_app(settings_of(WorkerSettings, **UNREACHABLE), init_observability=False)
    )
    assert (status, body["failing"]) == (503, ["postgres", "redis", "temporal"])


async def test_build_container_exposes_temporal_connector() -> None:
    async with build_container(settings_of(WorkerSettings, temporal_host="temporal:7233")) as container:
        assert container.temporal.host == "temporal:7233"
        assert list(container.readiness_checks()) == ["postgres", "redis", "temporal"]


async def test_registered_workers_run_and_shut_down_with_the_app() -> None:
    worker = FakeWorker()
    app = worker_app(
        registrations=[TaskQueueRegistration("va-main", workflows=[], activities=[])],
        worker_factory=Factory(worker),
    )
    async with make_client(app) as client:
        await asyncio.wait_for(worker.started.wait(), timeout=5)
        assert (await client.get("/healthz")).status_code == 200
    assert worker.shutdowns == 1
    assert not app.state.stop.requested


async def test_crashed_worker_fails_liveness_and_requests_stop() -> None:
    worker = FakeWorker(RuntimeError("poller died"))
    app = worker_app(
        registrations=[TaskQueueRegistration("va-main", workflows=[], activities=[])],
        worker_factory=Factory(worker),
    )
    async with make_client(app) as client:
        await asyncio.wait_for(app.state.stop.wait(), timeout=5)
        response = await client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["reason"] == "temporal_worker_stopped"
    assert (app.state.stop.reason, app.state.stop.failed) == ("task_failed:temporal_workers", True)


def test_main_parses_args_and_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = capture_serve(monkeypatch, worker_main)
    assert worker_main.main([]) == 0
    [(app, bind, kwargs)] = seen
    assert bind == Bind("0.0.0.0", 9300)  # noqa: S104
    assert kwargs == {"stop": app.state.stop}
