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

import httpx
from redis.asyncio import Redis
from starlette.applications import Starlette
from temporalio.contrib.pydantic import pydantic_data_converter

from va_common.checks import Dependencies, TemporalConnector, open_dependencies
from va_common.crypto import Envelope, KmsProvider, open_kms_provider
from va_common.health import CheckFn
from va_common.http import http_client
from va_common.metrics import Service, ServiceMetrics, build_metrics
from va_common.net import system_resolver
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
from va_common.storage import RecordingStorage, open_object_store, sse_for
from va_common.time import SystemClock
from va_db.org_settings import OrgSettingsReader
from va_db.repositories import OrgDataKeyStore
from va_db.session import Database
from va_db.telnyx_keys import TelnyxKeyResolver
from va_elevenlabs import ElevenLabsRest
from va_llm import LlmMetrics
from va_llm.platform import platform_gateway
from va_pathways.conversation import TextDeps
from va_pathways.runs import insecure_urls_allowed
from va_telnyx.settings import telnyx_factory, telnyx_http
from va_tools.http import tools_client
from va_voice.phrase_cache import PhraseCache
from va_worker import __version__
from va_worker.activities import WorkerActivities, main_registrations
from va_worker.runner import ClientSource, TemporalWorkers, WorkerFactory, temporal_worker
from va_worker.settings import WorkerSettings
from va_worker.task_queues import TaskQueueRegistration
from va_workflows.activities.prerender import OpenerPrerender

__all__ = [
    "ELEVENLABS_HTTP_TIMEOUT",
    "LLM_HTTP_TIMEOUT",
    "METRICS_PORT",
    "Container",
    "build_container",
    "create_app",
    "main",
]

METRICS_PORT: Final = 9300
ELEVENLABS_HTTP_TIMEOUT: Final = httpx.Timeout(15.0, connect=2.0)
"""Socket bound of the ElevenLabs REST client; each operation's own deadline (specs/15 §7) is shorter."""
LLM_HTTP_TIMEOUT: Final = httpx.Timeout(30.0, connect=2.0)
"""Socket bound of the LLM client of pathway test runs; the gateway's per-call deadlines are shorter."""


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
async def build_container(
    settings: WorkerSettings, metrics: ServiceMetrics | None = None
) -> AsyncIterator[Container]:
    """Postgres engine, Redis client, Temporal connector (pydantic converter), the database, the envelope,
    the Telnyx client factory (BYO keys through :class:`TelnyxKeyResolver`), the opener pre-render (ElevenLabs
    REST + phrase cache + org settings reader), the object store (recordings), the pathway test runs' text
    dependencies (LLM gateway with org providers, guarded tool HTTP client), and the activity instances;
    ``metrics`` is the registry the probes serve (a fresh one when ``None``).

    No network I/O until used. ``RuntimeError`` before anything is opened when ``STREAM_TOKEN_SECRET`` or
    ``TELNYX_CALL_CONTROL_APP_ID`` is empty, or (local KMS provider) ``LOCAL_MASTER_KEY`` is not configured.
    """
    dial = settings.dial_settings()
    service_metrics = metrics if metrics is not None else build_metrics(Service.WORKER)
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
        resolver = TelnyxKeyResolver.for_provider(database, kms_provider)
        factory = telnyx_factory(settings, http=http, redis=redis, resolve_key=resolver, clock=clock)
        store = await stack.enter_async_context(open_object_store(settings))
        activities = WorkerActivities.build(
            database=database,
            redis=redis,
            telnyx=factory,
            dial=dial,
            clock=clock,
            prerender=await stack.enter_async_context(_opener_prerender(settings, database, redis, clock)),
            storage=RecordingStorage(store, settings.s3_bucket_recordings, sse_for(settings)),
            postcall_step_ms=service_metrics.histogram("va_postcall_duration_ms"),
            text=await stack.enter_async_context(
                _text_deps(settings, database, kms_provider, clock, service_metrics)
            ),
        )
        yield Container(
            temporal=temporal,
            dependencies=deps,
            database=database,
            redis=redis,
            activities=activities,
            registrations=main_registrations(settings, activities),
        )


@asynccontextmanager
async def _opener_prerender(
    settings: WorkerSettings, database: Database, redis: Redis, clock: SystemClock
) -> AsyncIterator[OpenerPrerender]:
    """The phrase cache over ElevenLabs REST (its HTTP client and renders in flight are closed on exit)."""
    http = http_client(settings, timeout=ELEVENLABS_HTTP_TIMEOUT)
    rest = ElevenLabsRest(
        settings.elevenlabs_api_key.get_secret_value(), settings.elevenlabs_api_base, http, clock=clock
    )
    cache = PhraseCache(rest, redis)
    try:
        yield OpenerPrerender(
            cache=cache,
            org_settings=OrgSettingsReader(database, redis),
            model_id=settings.elevenlabs_tts_model,
        )
    finally:
        await cache.aclose()
        await http.aclose()


@asynccontextmanager
async def _text_deps(
    settings: WorkerSettings,
    database: Database,
    kms_provider: KmsProvider,
    clock: SystemClock,
    metrics: ServiceMetrics,
) -> AsyncIterator[TextDeps]:
    """The LLM gateway (``va_llm_*`` metrics on the probe registry) and the guarded tool HTTP client the
    pathway unit-test runs use (both HTTP clients are closed on exit)."""
    async with AsyncExitStack() as stack:
        llm_http = http_client(settings, timeout=LLM_HTTP_TIMEOUT)
        stack.push_async_callback(llm_http.aclose)
        gateway = platform_gateway(
            settings,
            database=database,
            envelope=Envelope(kms_provider, OrgDataKeyStore(database)),
            http=llm_http,
            clock=clock,
            metrics=LlmMetrics(metrics.registry),
        )
        tools_http = tools_client(settings, resolver=system_resolver)
        stack.push_async_callback(tools_http.aclose)
        yield TextDeps(
            gateway=gateway,
            tools_http=tools_http,
            clock=clock,
            allow_http=insecure_urls_allowed(settings.va_env),
        )


def create_app(  # noqa: PLR0913 - keyword-only test seams
    settings: WorkerSettings | None = None,
    *,
    container_factory: ContainerFactory | None = None,
    init_observability: bool = True,
    readiness_timeout_s: float = 1.0,
    worker_factory: WorkerFactory = temporal_worker,
    stop: StopController | None = None,
) -> Starlette:
    """Build the probe app whose lifespan runs the Temporal workers (the default container observes into the
    registry the probes serve)."""
    cfg = settings if settings is not None else WorkerSettings()
    controller = stop if stop is not None else StopController()
    workers = TemporalWorkers(worker_factory=worker_factory)
    metrics = build_metrics(Service.WORKER)
    factory: ContainerFactory = (
        container_factory if container_factory is not None else partial(build_container, metrics=metrics)
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with service_scope(
            app.state, cfg, factory, version=__version__, init_observability=init_observability
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
        metrics.registry,
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
