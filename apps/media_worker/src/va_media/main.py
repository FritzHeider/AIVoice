"""Media worker application and entry point (specs/01-architecture.md §1: ``python -m va_media.main``, 8200).

Probes per specs/27-observability.md §9: liveness fails while the event loop lags 500 ms or more; readiness
checks ``draining``, ``capacity`` (90 % of ``MEDIA_MAX_CALLS_PER_POD``), ``redis``, ``vad`` in that order.
SIGTERM starts the drain of specs/01-architecture.md §3 and specs/07 §19: the pod joins
``media:pods:draining``, readiness fails, new streams are refused, every live session gets a ``drain_notice``,
the listener stays open while calls remain (up to ``MEDIA_DRAIN_TIMEOUT_S`` of the injected clock); the
sessions still running then end with ``end_reason='worker_lost'`` and the server exits after their cleanup (at
most 10 s more). ``/ws/telnyx`` (``va_media.server``) attaches Telnyx media streams; the pod publishes its
load to ``media:pods`` every 2 s (``va_media.pods``); one pod-wide pub/sub connection carries every call's
control and carrier channels (``va_media.channels``); every attached call runs the pipeline of
``va_media.runtime`` (ElevenLabs STT/TTS, Silero VAD, the LLM gateway, the task-mode Brain) and, when the
lifespan built that runtime, its end-of-call routine (``va_media.call_end``). The lifespan
refuses to start without ``TELNYX_PUBLIC_KEY`` (``local`` falls back to the simulator's key file) or
``STREAM_TOKEN_SECRET``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

import uvloop
from redis.asyncio import Redis
from starlette.applications import Starlette

from va_common.checks import Dependencies, open_dependencies
from va_common.gc_tuning import GcPauseLog, installed_pause_log
from va_common.gc_tuning import tune_gc as tune_collector
from va_common.health import CheckFn
from va_common.ids import new_id
from va_common.logging import get_logger
from va_common.metrics import Service, ServiceMetrics, build_metrics
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
from va_common.stream_tokens import stream_token_secrets
from va_common.time import Clock, SystemClock
from va_db.call_context import CallContextStore
from va_db.session import Database
from va_domain.control import DrainNoticeControl
from va_media import __version__
from va_media.auth import MediaAuth
from va_media.calls import CallRegistry
from va_media.channels import ChannelHub
from va_media.context import CallContextLoader, DbCallContextLoader
from va_media.control import ControlHandler, DefaultControlHandler
from va_media.drain import DrainState
from va_media.loop_lag import LoopLagMonitor
from va_media.pods import PodRegistry
from va_media.runtime import RuntimeFactory, open_runtime
from va_media.server import MediaAttach, telnyx_media_route
from va_media.session import SessionBody, SessionEnder
from va_media.sessions import LiveSessions
from va_media.settings import MediaSettings
from va_media.vad_model import VadModelError, VadStatus, verify_vad_model
from va_telnyx.signatures import load_public_key

__all__ = [
    "DEFAULT_MODELS_DIR",
    "DEFAULT_PORT",
    "DRAIN_END_GRACE_S",
    "Container",
    "ContextLoaderFactory",
    "MediaState",
    "build_container",
    "create_app",
    "db_context_loader",
    "drain",
    "main",
    "media_auth",
]

DEFAULT_PORT: Final = 8200
GC_THRESHOLDS: Final = (10_000, 5, 50)
"""Collector thresholds of a serving media worker (generation 0, 1, 2; specs/07 §9 Decision P08-FIX-3)."""
DEFAULT_MODELS_DIR: Final = Path("models")
DRAIN_END_GRACE_S: Final = 10.0
"""How long the drain waits for sessions ended at the drain timeout to finish their cleanup."""

_log = get_logger(__name__)


class MediaDependencies(Protocol):
    @property
    def redis(self) -> Redis: ...

    def readiness_checks(self) -> Mapping[str, CheckFn]: ...


type ContainerFactory = Callable[[MediaSettings], AbstractAsyncContextManager[MediaDependencies]]
type ContextLoaderFactory = Callable[[MediaDependencies, Clock], CallContextLoader]


@dataclass(slots=True)
class Container:
    redis: Redis
    database: Database
    dependencies: Dependencies

    def readiness_checks(self) -> Mapping[str, CheckFn]:
        return self.dependencies.readiness_checks()


@asynccontextmanager
async def build_container(settings: MediaSettings) -> AsyncIterator[Container]:
    """The Redis client and the database (no network I/O until used; readiness does not check Postgres,
    specs/07 §2)."""
    async with open_dependencies() as deps, AsyncExitStack() as stack:
        database = Database.from_settings(settings)
        stack.push_async_callback(database.dispose)
        yield Container(deps.redis(settings), database, deps)


def db_context_loader(container: MediaDependencies, clock: Clock) -> CallContextLoader:
    """The production loader over the container's ``database``; ``TypeError`` when it has none."""
    database = getattr(container, "database", None)
    if not isinstance(database, Database):
        msg = "the media container has no database"
        raise TypeError(msg)
    return DbCallContextLoader(database, CallContextStore(container.redis), clock)


@dataclass(slots=True)
class MediaState:
    """Per-process media state shared by the probes, the drain, and (later) the call handlers."""

    settings: MediaSettings
    registry: CallRegistry
    drain: DrainState
    vad: VadStatus
    monitor: LoopLagMonitor
    clock: Clock
    sessions: LiveSessions


def media_auth(settings: MediaSettings) -> MediaAuth:
    """The handshake verifier; ``RuntimeError`` when the public key or the stream secret is missing (checked
    before anything opens), ``ValueError`` when the key is malformed."""
    key = settings.webhook_public_key()
    if not key:
        msg = "TELNYX_PUBLIC_KEY is not configured"
        raise RuntimeError(msg)
    if not settings.stream_token_secret.get_secret_value():
        msg = "STREAM_TOKEN_SECRET is not configured"
        raise RuntimeError(msg)
    return MediaAuth(
        scheme=settings.media_ws_scheme,
        host=settings.media_public_host,
        public_key=load_public_key(key),
        secrets=stream_token_secrets(settings),
    )


@runtime_checkable
class _EndingBody(Protocol):
    """A session body with an end-of-call routine (:class:`~va_media.runtime.CallRuntime`)."""

    @property
    def end_routine(self) -> SessionEnder | None: ...


@dataclass(frozen=True, slots=True)
class _Seams:
    body: SessionBody | None
    runtime: RuntimeFactory
    context_loader: ContextLoaderFactory
    control_handler: ControlHandler


def _attach(  # noqa: PLR0913 - collaborators built by the lifespan
    media: MediaState,
    auth: MediaAuth,
    container: MediaDependencies,
    hub: ChannelHub,
    *,
    metrics: ServiceMetrics,
    seams: _Seams,
    body: SessionBody,
) -> MediaAttach:
    ender = body.end_routine if isinstance(body, _EndingBody) else None
    return MediaAttach(
        auth=auth,
        registry=media.registry,
        drain=media.drain,
        redis=container.redis,
        pod_id=media.settings.pod_id,
        clock=media.clock,
        metrics=metrics,
        body=body,
        loader=seams.context_loader(container, media.clock),
        hub=hub,
        sessions=media.sessions,
        control_handler=seams.control_handler,
        ender=ender,
    )


async def _load_vad(vad: VadStatus, models_dir: Path) -> None:
    try:
        await verify_vad_model(models_dir)
    except VadModelError as exc:
        vad.error = str(exc)
        _log.error("media.vad_model_unavailable", detail=vad.error)
        return
    vad.loaded = True


def _readiness(
    media: MediaState, dependency_checks: Callable[[], Mapping[str, CheckFn]]
) -> dict[str, CheckFn]:
    return {
        "draining": media.drain.check,
        "capacity": media.registry.capacity_check,
        **dependency_checks(),
        "vad": media.vad.check,
    }


def create_app(  # noqa: PLR0913 - keyword-only test seams
    settings: MediaSettings | None = None,
    *,
    container_factory: ContainerFactory = build_container,
    init_observability: bool = True,
    readiness_timeout_s: float = 1.0,
    models_dir: Path = DEFAULT_MODELS_DIR,
    monitor: LoopLagMonitor | None = None,
    stop: StopController | None = None,
    clock: Clock | None = None,
    body: SessionBody | None = None,
    runtime: RuntimeFactory = open_runtime,
    context_loader: ContextLoaderFactory = db_context_loader,
    control_handler: ControlHandler | None = None,
) -> Starlette:
    """Build the media worker app; a crashed background task requests ``stop`` with a failure exit.

    ``body`` runs each attached call; without one the lifespan enters ``runtime`` (default
    :func:`~va_media.runtime.open_runtime`: the pipeline with the vendor sockets, the LLM gateway and the
    task-mode Brain) and uses the body it yields. A body with an ``end_routine`` (the runtime) gives every
    session that end-of-call routine. ``context_loader`` builds the call context loader from the
    container, ``control_handler`` handles control messages.
    """
    cfg = settings if settings is not None else MediaSettings()
    controller = stop if stop is not None else StopController()
    app_clock: Clock = clock if clock is not None else SystemClock()
    metrics = build_metrics(Service.MEDIA_WORKER)
    media = MediaState(
        settings=cfg,
        registry=CallRegistry(
            cfg.media_max_calls_per_pod,
            gauge=metrics.gauge("va_media_active_calls").labels(transport="telnyx"),
        ),
        drain=DrainState(metrics.gauge("va_media_draining")),
        vad=VadStatus(),
        monitor=monitor or LoopLagMonitor(histogram=metrics.histogram("va_event_loop_lag_ms")),
        clock=app_clock,
        sessions=LiveSessions(),
    )
    seams = _Seams(
        body=body,
        runtime=runtime,
        context_loader=context_loader,
        control_handler=control_handler if control_handler is not None else DefaultControlHandler(),
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        auth = media_auth(cfg)
        async with (
            service_scope(
                app.state, cfg, container_factory, version=__version__, init_observability=init_observability
            ) as container,
            AsyncExitStack() as stack,
        ):
            await _load_vad(media.vad, models_dir)
            body_of_call = seams.body
            if body_of_call is None:
                body_of_call = await stack.enter_async_context(
                    seams.runtime(
                        container, cfg, clock=app_clock, metrics=metrics, vad=media.vad, models_dir=models_dir
                    )
                )
            hub = ChannelHub(container.redis, app_clock)
            app.state.media_attach = _attach(
                media, auth, container, hub, metrics=metrics, seams=seams, body=body_of_call
            )
            pods = PodRegistry(
                container.redis, cfg.pod_id, calls=media.registry, drain=media.drain, clock=app_clock
            )
            supervisor = Supervisor(controller.task_failed)
            supervisor.start("loop_lag", media.monitor.run)
            pauses = installed_pause_log()
            if pauses is not None:
                supervisor.start("gc_pauses", pauses.run)
            supervisor.start("media_pods", pods.run)
            supervisor.start("channels", hub.run)
            try:
                yield
            finally:
                await supervisor.aclose()
                await hub.aclose()
                await pods.remove()
                await media.drain.clear(container.redis, cfg.pod_id)

    app = probe_app(lifespan, routes=[telnyx_media_route()])
    app.state.media = media
    app.state.stop = controller
    app.state.probes = ProbeState(
        __version__,
        metrics.registry,
        checks=partial(_readiness, media, container_checks(app.state)),
        liveness=media.monitor.liveness,
        readiness_timeout_s=readiness_timeout_s,
    )
    return app


async def drain(app: Starlette) -> None:
    """Mark the pod draining and notify every session, then wait (listener open) until its calls end or the
    drain timeout elapses; at the timeout the remaining sessions end with ``worker_lost``."""
    media: MediaState = app.state.media
    container: MediaDependencies = app.state.container
    await media.drain.begin(container.redis, media.settings.pod_id)
    for session in media.sessions.all():
        session.notify(DrainNoticeControl(id=new_id()))
    if await media.registry.wait_idle(media.settings.media_drain_timeout_s, clock=media.clock):
        return
    remaining = media.sessions.all()
    _log.warning("media.drain_timeout", active_calls=media.registry.active, ended_sessions=len(remaining))
    for session in remaining:
        session.request_end("worker_lost", "error")
    if not await media.sessions.wait_idle(media.clock, DRAIN_END_GRACE_S):
        _log.error("media.drain_unfinished", sessions=len(media.sessions))


def tune_gc() -> GcPauseLog:
    """Keep the cyclic collector out of the media loop's way (specs/07 §21 Decision P05-T17e2, §9 Decision
    P08-FIX-3): :func:`va_common.gc_tuning.tune_gc` with :data:`GC_THRESHOLDS`.

    Startup objects move to the permanent generation, so no later collection walks them. Generation 0 collects
    every 10 000 net allocations instead of 700: per-frame objects are freed by reference counting, and a pass
    stays short (a 50 000 threshold let one pass walk and free ~50 000 objects, 40-75 ms, which underran every
    speaking leg); generation 1 is walked after five generation-0 passes, so it stays small too.
    """
    return tune_collector(GC_THRESHOLDS)


def main(argv: Sequence[str] | None = None) -> int:
    bind = parse_bind(argv, default_port=DEFAULT_PORT, description="VoiceAgents media worker")
    stop = StopController()
    app = create_app(stop=stop)
    tune_gc()
    return serve(app, bind, stop=stop, drain=partial(drain, app), loop_factory=uvloop.new_event_loop)


if __name__ == "__main__":
    raise SystemExit(main())
