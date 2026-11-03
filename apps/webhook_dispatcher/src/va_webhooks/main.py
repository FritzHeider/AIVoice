"""Webhook dispatcher entry point (specs/01-architecture.md §1: ``python -m va_webhooks.main``, metrics 9400).

Readiness checks Postgres only (specs/27-observability.md §9). The lifespan supervises the outbox delivery
loop of specs/19-events-and-webhooks.md §8 (:class:`va_webhooks.dispatcher.Dispatcher`); SIGTERM stops
claiming and waits up to 20 s for in-flight attempts (unfinished rows recover through their lease).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial
from typing import Final

from starlette.applications import Starlette

from va_common.metrics import Service, ServiceMetrics, build_metrics
from va_common.service import (
    ContainerFactory,
    ProbeState,
    ReadinessSource,
    StopController,
    Supervisor,
    container_checks,
    parse_bind,
    probe_app,
    serve,
    service_scope,
)
from va_webhooks import __version__
from va_webhooks.container import build_container
from va_webhooks.dispatcher import Dispatcher
from va_webhooks.settings import DispatcherSettings

__all__ = ["DISPATCH_TASK", "METRICS_PORT", "build_container", "create_app", "main"]

METRICS_PORT: Final = 9400
DISPATCH_TASK: Final = "webhook_dispatcher"


class DispatchRun:
    """The running delivery loop of one lifespan: :meth:`drain` stops claiming and waits for it."""

    def __init__(self) -> None:
        self.halt = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    async def drain(self) -> None:
        self.halt.set()
        if self.task is not None:
            await asyncio.wait({self.task})


def create_app(
    settings: DispatcherSettings | None = None,
    *,
    container_factory: ContainerFactory[DispatcherSettings] | None = None,
    init_observability: bool = True,
    readiness_timeout_s: float = 1.0,
    stop: StopController | None = None,
) -> Starlette:
    """Build the probe app whose lifespan runs the dispatcher (the default container observes into the
    registry the probes serve); a container without a ``dispatcher`` only serves the probes."""
    cfg = settings if settings is not None else DispatcherSettings()
    controller = stop if stop is not None else StopController()
    metrics = build_metrics(Service.WEBHOOK_DISPATCHER)
    factory: ContainerFactory[DispatcherSettings] = (
        container_factory if container_factory is not None else _default_factory(metrics)
    )
    run = DispatchRun()

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with service_scope(
            app.state, cfg, factory, version=__version__, init_observability=init_observability
        ) as container:
            supervisor = Supervisor(controller.task_failed)
            dispatcher = getattr(container, "dispatcher", None)
            if isinstance(dispatcher, Dispatcher):
                run.halt.clear()
                run.task = supervisor.start(DISPATCH_TASK, partial(dispatcher.run, run.halt))
            try:
                yield
            finally:
                run.halt.set()
                await supervisor.aclose()

    app = probe_app(lifespan)
    app.state.stop = controller
    app.state.dispatch = run
    app.state.probes = ProbeState(
        __version__,
        metrics.registry,
        checks=container_checks(app.state),
        readiness_timeout_s=readiness_timeout_s,
    )
    return app


def _default_factory(metrics: ServiceMetrics) -> ContainerFactory[DispatcherSettings]:
    def factory(settings: DispatcherSettings) -> AbstractAsyncContextManager[ReadinessSource]:
        return build_container(settings, metrics=metrics)

    return factory


def main(argv: Sequence[str] | None = None) -> int:
    bind = parse_bind(argv, default_port=METRICS_PORT, description="VoiceAgents webhook dispatcher")
    stop = StopController()
    app = create_app(stop=stop)
    return serve(app, bind, stop=stop, drain=app.state.dispatch.drain)


if __name__ == "__main__":
    raise SystemExit(main())
