"""Telephony edge application factory (specs/01-architecture.md §1: ``uvicorn va_edge.main:app``, port 8100).

The app serves the Telnyx webhook routes (specs/06-telephony-telnyx.md §4, :mod:`va_edge.intake`), the probes
of specs/27-observability.md §9 (readiness: Postgres, Redis, Temporal), and the telephony-edge metrics catalog
(§3.2). Stored voice events are applied by :class:`va_edge.carrier.CarrierEventHandler` (specs/06 §5) unless
a test passes its own ``handlers``. The lifespan refuses to start without a valid ``TELNYX_PUBLIC_KEY`` (in
``local`` the simulator's key file is the fallback) and runs the event processor and the reprocessor as
supervised tasks; a crash of either stops the process. Importing this module performs no I/O.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI
from nacl.signing import VerifyKey

from va_common.metrics import Service, ServiceMetrics, build_metrics
from va_common.service import (
    ProbeState,
    StopController,
    Supervisor,
    container_checks,
    probe_routes,
    service_scope,
)
from va_common.time import Clock, SystemClock
from va_edge import __version__
from va_edge.carrier import CarrierEventHandler
from va_edge.container import EdgeContainerFactory, EdgeDependencies, build_container
from va_edge.intake import WebhookIntake, db_key_lookup, telnyx_routes
from va_edge.keys import WebhookKeys
from va_edge.processor import EventHandler, EventProcessor
from va_edge.reprocessor import TelnyxEventReprocessor
from va_edge.settings import EdgeSettings
from va_edge.signals import TemporalCarrierSignaller
from va_events.call_events import DbCallEventWriter, DirectCallEventWriter
from va_events.publisher import EventPublisher
from va_telnyx.signatures import load_public_key

__all__ = ["TITLE", "app", "build_container", "carrier_handlers", "create_app", "platform_key"]

TITLE: Final = "VoiceAgents Telephony Edge"


def platform_key(settings: EdgeSettings) -> VerifyKey:
    """The platform webhook key; ``RuntimeError`` when unset, ``ValueError`` when malformed."""
    key = settings.webhook_public_key()
    if not key:
        msg = "TELNYX_PUBLIC_KEY is not configured"
        raise RuntimeError(msg)
    return load_public_key(key)


def carrier_handlers(
    container: EdgeDependencies, *, clock: Clock, metrics: ServiceMetrics
) -> Mapping[str, EventHandler]:
    """The production registry: :class:`CarrierEventHandler` for every voice event type, publishing domain
    events with direct ``call_events`` writes and signalling ``CallWorkflow`` through Temporal."""
    database = container.database
    publisher = EventPublisher(
        container.redis, call_events=DirectCallEventWriter(DbCallEventWriter(database))
    )
    handler = CarrierEventHandler(
        database,
        container.redis,
        publisher,
        TemporalCarrierSignaller(container.temporal),
        clock=clock,
        metrics=metrics,
    )
    return handler.handlers()


def create_app(  # noqa: PLR0913 - keyword-only test seams
    settings: EdgeSettings | None = None,
    *,
    container_factory: EdgeContainerFactory = build_container,
    init_observability: bool = True,
    readiness_timeout_s: float = 1.0,
    handlers: Mapping[str, EventHandler] | None = None,
    clock: Clock | None = None,
    stop: StopController | None = None,
) -> FastAPI:
    """Build the edge application; every keyword argument after ``settings`` is a test seam."""
    cfg = settings if settings is not None else EdgeSettings()
    edge_clock = clock if clock is not None else SystemClock()
    controller = stop if stop is not None else StopController()
    metrics = build_metrics(Service.TELEPHONY_EDGE)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        key = platform_key(cfg)
        async with service_scope(
            app.state, cfg, container_factory, version=__version__, init_observability=init_observability
        ) as container:
            processor, supervisor = _serve_webhooks(app, container, key)
            try:
                yield
            finally:
                processor.close()
                await supervisor.aclose()

    def _serve_webhooks(
        app: FastAPI, container: EdgeDependencies, key: VerifyKey
    ) -> tuple[EventProcessor, Supervisor]:
        database = container.database
        registry = (
            handlers
            if handlers is not None
            else carrier_handlers(container, clock=edge_clock, metrics=metrics)
        )
        processor = EventProcessor(database, clock=edge_clock, metrics=metrics, handlers=registry)
        reprocessor = TelnyxEventReprocessor(database, processor, clock=edge_clock)
        keys = WebhookKeys(key, db_key_lookup(database), clock=edge_clock)
        app.state.processor = processor
        app.state.reprocessor = reprocessor
        app.state.intake = WebhookIntake(database, keys, processor, clock=edge_clock, metrics=metrics)
        app.state.supervisor = supervisor = Supervisor(controller.task_failed)
        supervisor.start("telnyx_event_processor", processor.run)
        supervisor.start("telnyx_event_reprocessor", reprocessor.run)
        return processor, supervisor

    app = FastAPI(
        title=TITLE, version=__version__, openapi_url=None, docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.router.routes.extend([*probe_routes(), *telnyx_routes()])
    app.state.settings = cfg
    app.state.stop = controller
    app.state.metrics = metrics
    app.state.probes = ProbeState(
        __version__,
        metrics.registry,
        checks=container_checks(app.state),
        readiness_timeout_s=readiness_timeout_s,
    )
    return app


app = create_app()
