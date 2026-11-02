"""Worker entry point (specs/01-architecture.md §1: ``python -m va_worker.main``, probes on port 9300).

Readiness checks Postgres, Redis, and Temporal (specs/27-observability.md §9); liveness fails when a started
Temporal worker stopped. The main task queue runs ``CallWorkflow`` with its activities (specs/01 §1 Decision
P05-T06d); the Temporal client uses the pydantic data converter. A crashed worker task stops the process with
exit code 1 (the orchestrator restarts it).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import Final, Protocol

from redis.asyncio import Redis
from starlette.applications import Starlette
from temporalio.contrib.pydantic import pydantic_data_converter

from va_common.checks import Dependencies, TemporalConnector, open_dependencies
from va_common.crypto import Envelope, open_kms_provider
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
from va_common.time import SystemClock
from va_db.repositories import OrgDataKeyStore
from va_db.session import Database
from va_db.telnyx_keys import TelnyxKeyResolver
from va_worker import __version__
from va_worker.activities import WorkerActivities, main_registrations, telnyx_factory, telnyx_http
from va_worker.runner import ClientSource, TemporalWorkers, WorkerFactory, temporal_worker
from va_worker.settings import WorkerSettings
from va_worker.task_queues import TaskQueueRegistration

__all__ = ["METRICS_PORT", "Container", "build_container", "create_app", "main"]

METRICS_PORT: Final = 9300


class WorkerDependencies(Protocol):
    @property
    def temporal(self) -> ClientSource: ...

    @property
    def registrations(self) -> Sequence[TaskQueueRegistration]: ...

    def readiness_checks(self) -> Mapping[str, CheckFn]: ...


type ContainerFactory = Callable[[WorkerSettings], AbstractAsyncContextManager[WorkerDependencies]]


@dataclass(slots=True)
class Container:
    temporal: TemporalConnector
    dependencies: Dependencies
    database: Database
    redis: Redis
    activities: WorkerActivities
    registrations: tuple[TaskQueueRegistration, ...]

    def readiness_checks(self) -> Mapping[str, CheckFn]:
        return self.dependencies.readiness_checks()


@asynccontextmanager
async def build_container(settings: WorkerSettings) -> AsyncIterator[Container]:
    """Postgres engine, Redis client, Temporal connector (pydantic converter), the database, the envelope,
    the Telnyx client factory (BYO keys through :class:`TelnyxKeyResolver`), and the activity instances.

    No network I/O until used. ``RuntimeError`` before anything is opened when ``STREAM_TOKEN_SECRET`` or
    ``TELNYX_CALL_CONTROL_APP_ID`` is empty, or (local KMS provider) ``LOCAL_MASTER_KEY`` is not configured.
    """
    dial = settings.dial_settings()
    async with (
        open_kms_provider(settings) as kms_provider,
        open_dependencies() as deps,
        AsyncExitStack() as stack,
    ):
        deps.postgres(settings)
        redis = deps.redis(settings)
        temporal = deps.temporal(settings, data_converter=pydantic_data_converter)
        database = Database.from_settings(settings)
        stack.push_async_callback(database.dispose)
        http = telnyx_http(settings)
        stack.push_async_callback(http.aclose)
        clock = SystemClock()
        envelope = Envelope(kms_provider, OrgDataKeyStore(database))
        factory = telnyx_factory(
            settings, http=http, redis=redis, resolve_key=TelnyxKeyResolver(database, envelope), clock=clock
        )
        activities = WorkerActivities.build(
            database=database, redis=redis, telnyx=factory, dial=dial, clock=clock
        )
        yield Container(
            temporal=temporal,
            dependencies=deps,
            database=database,
            redis=redis,
            activities=activities,
            registrations=main_registrations(settings, activities),
        )


def create_app(  # noqa: PLR0913 - keyword-only test seams
    settings: WorkerSettings | None = None,
    *,
    container_factory: ContainerFactory = build_container,
    init_observability: bool = True,
    readiness_timeout_s: float = 1.0,
    worker_factory: WorkerFactory = temporal_worker,
    stop: StopController | None = None,
) -> Starlette:
    """Build the probe app whose lifespan runs the Temporal workers."""
    cfg = settings if settings is not None else WorkerSettings()
    controller = stop if stop is not None else StopController()
    workers = TemporalWorkers(worker_factory=worker_factory)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with service_scope(
            app.state, cfg, container_factory, version=__version__, init_observability=init_observability
        ) as container:
            supervisor = Supervisor(controller.task_failed)
            supervisor.start(
                "temporal_workers", partial(workers.run, container.temporal, container.registrations)
            )
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
