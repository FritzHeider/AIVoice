"""Events sink entry point (specs/01-architecture.md §1: ``python -m va_events_sink.main``, metrics 9500).

Readiness checks Redis, ClickHouse (``GET /ping`` on ``CLICKHOUSE_URL``), and ``clickhouse_schema``
(specs/27-observability.md §9 Decision): a supervised startup task applies the ClickHouse migrations of
specs/03-data-model.md §12, and a fatal schema error stops the process with exit code 1. Once the schema is
applied a second supervised task runs the stream consumer (:func:`va_events_sink.sink.run_sink`, specs/19 §1
Decision P06-T05) over the container's Redis client.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any, Final

from starlette.applications import Starlette
from starlette.datastructures import State

from va_common.health import CheckFn
from va_common.metrics import Service, ServiceMetrics, build_metrics
from va_common.service import (
    Checks,
    ContainerFactory,
    ProbeState,
    StopController,
    Supervisor,
    container_checks,
    parse_bind,
    probe_app,
    serve,
    service_scope,
)
from va_common.time import Clock, SystemClock
from va_events.clickhouse import open_clickhouse
from va_events_sink import __version__
from va_events_sink.container import SinkContainer, build_container
from va_events_sink.schema import SchemaState, bootstrap_schema
from va_events_sink.settings import EventsSinkSettings
from va_events_sink.sink import SinkConnect, run_sink

__all__ = [
    "METRICS_PORT",
    "SCHEMA_CHECK",
    "SINK_TASK",
    "SinkContext",
    "SinkRunner",
    "build_container",
    "create_app",
    "default_sink",
    "main",
]

METRICS_PORT: Final = 9500
SCHEMA_CHECK: Final = "clickhouse_schema"
SINK_TASK: Final = "events_sink"


@dataclass(frozen=True, slots=True)
class SinkContext:
    """What the lifespan hands the stream consumer besides the container."""

    connect: SinkConnect
    schema: SchemaState
    clock: Clock
    metrics: ServiceMetrics


type SinkRunner = Callable[[Any, SinkContext], Coroutine[Any, Any, None]]


async def default_sink(container: SinkContainer, context: SinkContext) -> None:
    """The production consumer: :func:`run_sink` on the container's Redis client."""
    await run_sink(
        container.redis, context.connect, context.schema, clock=context.clock, metrics=context.metrics
    )


def sink_checks(state: State) -> Checks:
    """The container's checks followed by ``clickhouse_schema`` of the running lifespan."""
    dependency_checks = container_checks(state)

    def checks() -> Mapping[str, CheckFn]:
        schema: SchemaState = state.schema
        return {**dependency_checks(), SCHEMA_CHECK: schema.check}

    return checks


def create_app(  # noqa: PLR0913 - keyword-only test seams
    settings: EventsSinkSettings | None = None,
    *,
    container_factory: ContainerFactory[EventsSinkSettings] = build_container,
    init_observability: bool = True,
    readiness_timeout_s: float = 1.0,
    schema_connect: SinkConnect | None = None,
    clock: Clock | None = None,
    stop: StopController | None = None,
    sink: SinkRunner | None = default_sink,
) -> Starlette:
    """Build the probe app whose lifespan bootstraps the ClickHouse schema, then runs ``sink`` (``None``: no
    stream consumer)."""
    cfg = settings if settings is not None else EventsSinkSettings()
    controller = stop if stop is not None else StopController()
    connect = schema_connect if schema_connect is not None else partial(open_clickhouse, cfg.clickhouse_url)
    schema_clock = clock if clock is not None else SystemClock()
    metrics = build_metrics(Service.EVENTS_SINK)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with service_scope(
            app.state, cfg, container_factory, version=__version__, init_observability=init_observability
        ):
            app.state.schema = schema = SchemaState()
            supervisor = Supervisor(controller.task_failed)
            supervisor.start(SCHEMA_CHECK, partial(bootstrap_schema, connect, schema, clock=schema_clock))
            if sink is not None:
                context = SinkContext(connect, schema, schema_clock, metrics)
                supervisor.start(SINK_TASK, partial(sink, app.state.container, context))
            try:
                yield
            finally:
                await supervisor.aclose()

    app = probe_app(lifespan)
    app.state.stop = controller
    app.state.schema = SchemaState()
    app.state.probes = ProbeState(
        __version__,
        metrics.registry,
        checks=sink_checks(app.state),
        readiness_timeout_s=readiness_timeout_s,
    )
    return app


def main(argv: Sequence[str] | None = None) -> int:
    bind = parse_bind(argv, default_port=METRICS_PORT, description="VoiceAgents events sink")
    stop = StopController()
    return serve(create_app(stop=stop), bind, stop=stop)


if __name__ == "__main__":
    raise SystemExit(main())
