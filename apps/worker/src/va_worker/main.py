"""Worker entry point (specs/01-architecture.md §1: ``python -m va_worker.main``, probes on port 9300).

Readiness checks Postgres, Redis, and Temporal (specs/27-observability.md §9); liveness fails when a started
Temporal worker stopped. A crashed worker task stops the process with exit code 1 (the orchestrator restarts
it).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import Final, Protocol

from starlette.applications import Starlette

from va_common.checks import Dependencies, TemporalConnector, open_dependencies
from va_common.health import CheckFn
from va_common.metrics import Service, build_metrics
from va_common.service import (
    ProbeState,
    StopController,
    Supervisor,
    container_checks,
    parse_bind,
    probe_app,
    serve,
    service_scope,
)
from va_worker import __version__
from va_worker.runner import ClientSource, TemporalWorkers, WorkerFactory, temporal_worker
from va_worker.settings import WorkerSettings
from va_worker.task_queues import REGISTRATIONS, TaskQueueRegistration

__all__ = ["METRICS_PORT", "Container", "build_container", "create_app", "main"]

METRICS_PORT: Final = 9300


class WorkerDependencies(Protocol):
    @property
    def temporal(self) -> ClientSource: ...

    def readiness_checks(self) -> Mapping[str, CheckFn]: ...


type ContainerFactory = Callable[[WorkerSettings], AbstractAsyncContextManager[WorkerDependencies]]


@dataclass(slots=True)
class Container:
    temporal: TemporalConnector
    dependencies: Dependencies

    def readiness_checks(self) -> Mapping[str, CheckFn]:
        return self.dependencies.readiness_checks()


@asynccontextmanager
async def build_container(settings: WorkerSettings) -> AsyncIterator[Container]:
    """Postgres engine, Redis client, and Temporal connector (no network I/O until used)."""
    async with open_dependencies() as deps:
        deps.postgres(settings)
        deps.redis(settings)
        yield Container(deps.temporal(settings), deps)


def create_app(  # noqa: PLR0913 - keyword-only test seams
    settings: WorkerSettings | None = None,
    *,
    container_factory: ContainerFactory = build_container,
    init_observability: bool = True,
    readiness_timeout_s: float = 1.0,
    registrations: Sequence[TaskQueueRegistration] = REGISTRATIONS,
    worker_factory: WorkerFactory = temporal_worker,
    stop: StopController | None = None,
) -> Starlette:
    """Build the probe app whose lifespan runs the Temporal workers."""
    cfg = settings if settings is not None else WorkerSettings()
    controller = stop if stop is not None else StopController()
    workers = TemporalWorkers(registrations, worker_factory=worker_factory)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with service_scope(
            app.state, cfg, container_factory, version=__version__, init_observability=init_observability
        ) as container:
            supervisor = Supervisor(controller.task_failed)
            supervisor.start("temporal_workers", partial(workers.run, container.temporal))
            try:
                yield
            finally:
                await workers.shutdown()
                await supervisor.aclose()

    app = probe_app(lifespan)
    app.state.workers = workers
    app.state.stop = controller
    app.state.probes = ProbeState(
        __version__,
        build_metrics(Service.WORKER).registry,
        checks=container_checks(app.state),
        liveness=workers.liveness,
        readiness_timeout_s=readiness_timeout_s,
    )
    return app


def main(argv: Sequence[str] | None = None) -> int:
    bind = parse_bind(argv, default_port=METRICS_PORT, description="VoiceAgents Temporal worker")
    stop = StopController()
    return serve(create_app(stop=stop), bind, stop=stop)


if __name__ == "__main__":
    raise SystemExit(main())
