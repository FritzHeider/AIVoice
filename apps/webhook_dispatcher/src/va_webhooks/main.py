"""Webhook dispatcher entry point (specs/01-architecture.md §1: ``python -m va_webhooks.main``, metrics 9400).

Readiness checks Postgres only (specs/27-observability.md §9). The outbox delivery loop is added with the
webhooks phase (specs/19-events-and-webhooks.md §8).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Final

from starlette.applications import Starlette

from va_common.checks import Dependencies, open_dependencies
from va_common.metrics import Service, build_metrics
from va_common.service import ContainerFactory, parse_bind, probe_service_app, serve
from va_webhooks import __version__
from va_webhooks.settings import DispatcherSettings

__all__ = ["METRICS_PORT", "build_container", "create_app", "main"]

METRICS_PORT: Final = 9400


@asynccontextmanager
async def build_container(settings: DispatcherSettings) -> AsyncIterator[Dependencies]:
    """The Postgres engine (no network I/O until a check runs)."""
    async with open_dependencies() as deps:
        deps.postgres(settings)
        yield deps


def create_app(
    settings: DispatcherSettings | None = None,
    *,
    container_factory: ContainerFactory[DispatcherSettings] = build_container,
    init_observability: bool = True,
    readiness_timeout_s: float = 1.0,
) -> Starlette:
    return probe_service_app(
        settings if settings is not None else DispatcherSettings(),
        container_factory,
        registry=build_metrics(Service.WEBHOOK_DISPATCHER).registry,
        version=__version__,
        init_observability=init_observability,
        readiness_timeout_s=readiness_timeout_s,
    )


def main(argv: Sequence[str] | None = None) -> int:
    bind = parse_bind(argv, default_port=METRICS_PORT, description="VoiceAgents webhook dispatcher")
    return serve(create_app(), bind)


if __name__ == "__main__":
    raise SystemExit(main())
