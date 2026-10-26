"""Events sink entry point (specs/01-architecture.md §1: ``python -m va_events_sink.main``, metrics 9500).

Readiness checks Redis and ClickHouse (``GET /ping`` on ``CLICKHOUSE_URL``, specs/27-observability.md §9). The
stream consumer and ClickHouse schema bootstrap come with the analytics work (specs/03-data-model.md §12).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from starlette.applications import Starlette

from va_common.metrics import Service, build_metrics
from va_common.service import ContainerFactory, parse_bind, probe_service_app, serve
from va_events_sink import __version__
from va_events_sink.container import build_container
from va_events_sink.settings import EventsSinkSettings

__all__ = ["METRICS_PORT", "build_container", "create_app", "main"]

METRICS_PORT: Final = 9500


def create_app(
    settings: EventsSinkSettings | None = None,
    *,
    container_factory: ContainerFactory[EventsSinkSettings] = build_container,
    init_observability: bool = True,
    readiness_timeout_s: float = 1.0,
) -> Starlette:
    return probe_service_app(
        settings if settings is not None else EventsSinkSettings(),
        container_factory,
        registry=build_metrics(Service.EVENTS_SINK).registry,
        version=__version__,
        init_observability=init_observability,
        readiness_timeout_s=readiness_timeout_s,
    )


def main(argv: Sequence[str] | None = None) -> int:
    bind = parse_bind(argv, default_port=METRICS_PORT, description="VoiceAgents events sink")
    return serve(create_app(), bind)


if __name__ == "__main__":
    raise SystemExit(main())
