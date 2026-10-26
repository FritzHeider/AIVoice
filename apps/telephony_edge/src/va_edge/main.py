"""Telephony edge application factory (specs/01-architecture.md §1: ``uvicorn va_edge.main:app``, port 8100).

The app serves the probes of specs/27-observability.md §9 (readiness: Postgres, Redis, Temporal) and the
telephony-edge metrics catalog (§3.2); the Telnyx webhook routes are added by the telephony-edge phase
(specs/06-telephony-telnyx.md §4). Importing this module performs no I/O.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI

from va_common.checks import Dependencies, open_dependencies
from va_common.metrics import Service, build_metrics
from va_common.service import ContainerFactory, ProbeState, container_checks, probe_routes, service_lifespan
from va_edge import __version__
from va_edge.settings import EdgeSettings

__all__ = ["TITLE", "app", "build_container", "create_app"]

TITLE: Final = "VoiceAgents Telephony Edge"


@asynccontextmanager
async def build_container(settings: EdgeSettings) -> AsyncIterator[Dependencies]:
    """Postgres engine, Redis client, and Temporal connector (no network I/O until a check runs)."""
    async with open_dependencies() as deps:
        deps.postgres(settings)
        deps.redis(settings)
        deps.temporal(settings)
        yield deps


def create_app(
    settings: EdgeSettings | None = None,
    *,
    container_factory: ContainerFactory[EdgeSettings] = build_container,
    init_observability: bool = True,
    readiness_timeout_s: float = 1.0,
) -> FastAPI:
    """Build the edge application; ``container_factory``/``init_observability`` are test seams."""
    cfg = settings if settings is not None else EdgeSettings()
    lifespan = service_lifespan(
        cfg, container_factory, version=__version__, init_observability=init_observability
    )
    app = FastAPI(
        title=TITLE, version=__version__, openapi_url=None, docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.router.routes.extend(probe_routes())
    app.state.settings = cfg
    app.state.metrics = build_metrics(Service.TELEPHONY_EDGE)
    app.state.probes = ProbeState(
        __version__,
        app.state.metrics.registry,
        checks=container_checks(app.state),
        readiness_timeout_s=readiness_timeout_s,
    )
    return app


app = create_app()
