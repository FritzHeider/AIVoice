"""Events sink entry point (specs/01-architecture.md §1: ``python -m va_events_sink.main``, metrics 9500).

Readiness checks Redis, ClickHouse (``GET /ping`` on ``CLICKHOUSE_URL``), and ``clickhouse_schema``
(specs/27-observability.md §9 Decision): a supervised startup task applies the ClickHouse migrations of
specs/03-data-model.md §12, and a fatal schema error stops the process with exit code 1. The stream consumer
comes with the analytics work.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from functools import partial
from typing import Final

from starlette.applications import Starlette
from starlette.datastructures import State

from va_common.health import CheckFn
from va_common.metrics import Service, build_metrics
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
from va_events_sink.container import build_container
from va_events_sink.schema import SchemaConnect, SchemaState, bootstrap_schema
from va_events_sink.settings import EventsSinkSettings

__all__ = ["METRICS_PORT", "SCHEMA_CHECK", "build_container", "create_app", "main"]

METRICS_PORT: Final = 9500
SCHEMA_CHECK: Final = "clickhouse_schema"


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
    schema_connect: SchemaConnect | None = None,
    clock: Clock | None = None,
    stop: StopController | None = None,
) -> Starlette:
    """Build the probe app whose lifespan bootstraps the ClickHouse schema."""
    cfg = settings if settings is not None else EventsSinkSettings()
    controller = stop if stop is not None else StopController()
    connect = schema_connect if schema_connect is not None else partial(open_clickhouse, cfg.clickhouse_url)
    schema_clock = clock if clock is not None else SystemClock()

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with service_scope(
            app.state, cfg, container_factory, version=__version__, init_observability=init_observability
        ):
            app.state.schema = schema = SchemaState()
            supervisor = Supervisor(controller.task_failed)
            supervisor.start(SCHEMA_CHECK, partial(bootstrap_schema, connect, schema, clock=schema_clock))
            try:
                yield
            finally:
                await supervisor.aclose()

    app = probe_app(lifespan)
    app.state.stop = controller
    app.state.schema = SchemaState()
    app.state.probes = ProbeState(
        __version__,
        build_metrics(Service.EVENTS_SINK).registry,
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
