"""API application factory (specs/01-architecture.md §1: ``uvicorn va_api.main:app``, port 8000).

``create_app`` wires middleware (outer to inner: request context, metrics, rate-limit headers, idempotency
capture, CORS), the envelope-aware error handlers and the idempotent replay handler, the routers, and the
OpenAPI generators (``app.openapi()`` public, ``app.state.internal_openapi()`` for ``/internal/*``). The
lifespan initialises observability and opens the dependency container; importing this module performs no I/O.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import Final

from fastapi import APIRouter, FastAPI
from starlette.middleware.cors import CORSMiddleware

from va_api import __version__
from va_api.container import ContainerFactory, build_container
from va_api.errors import install_handlers
from va_api.idempotency import REPLAYED_HEADER, IdempotencyMiddleware, install_idempotency
from va_api.middleware import MetricsMiddleware, RequestContextMiddleware
from va_api.openapi import TITLE, build_internal_openapi, build_openapi
from va_api.rate_limit import RateLimitHeadersMiddleware
from va_api.routers import (
    auth_mfa,  # noqa: F401  (registers the MFA routes on internal_auth_router)
    auth_password,  # noqa: F401  (registers the password routes on internal_auth_router)
)
from va_api.routers.auth import internal_auth_router
from va_api.routers.health import probe_router, public_router
from va_api.routers.org_sso import org_router
from va_api.settings import ApiSettings
from va_common.metrics import Service, build_metrics
from va_common.service import ProbeState, container_checks, service_scope

__all__ = ["CORS_ALLOW_HEADERS", "CORS_ALLOW_METHODS", "CORS_EXPOSE_HEADERS", "ROUTERS", "app", "create_app"]

# specs/28-security.md §5
CORS_ALLOW_METHODS: Final = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
CORS_ALLOW_HEADERS: Final = [
    "Authorization",
    "Content-Type",
    "Idempotency-Key",
    "X-CSRF-Token",
    "X-VA-Org-Id",
    "Last-Event-ID",
]
CORS_EXPOSE_HEADERS: Final = [
    "X-Request-Id",
    "Retry-After",
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
    REPLAYED_HEADER,
]
# Routers carry their own prefix (route.path_format is then the full template used for metrics labels).
ROUTERS: Final[tuple[APIRouter, ...]] = (public_router, org_router, internal_auth_router, probe_router)


def create_app(
    settings: ApiSettings | None = None,
    *,
    container_factory: ContainerFactory = build_container,
    init_observability: bool = True,
    readiness_timeout_s: float = 1.0,
    routers: tuple[APIRouter, ...] = ROUTERS,
) -> FastAPI:
    """Build the API application; ``container_factory``/``init_observability`` are test seams."""
    cfg = settings if settings is not None else ApiSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with service_scope(
            app.state, cfg, container_factory, version=__version__, init_observability=init_observability
        ):
            yield

    app = FastAPI(title=TITLE, version=__version__, lifespan=lifespan)
    app.state.settings = cfg
    app.state.metrics = build_metrics(Service.API)
    app.state.probes = ProbeState(
        __version__,
        app.state.metrics.registry,
        checks=container_checks(app.state),
        readiness_timeout_s=readiness_timeout_s,
    )
    app.state.routers = routers
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[cfg.dashboard_base_url],
        allow_credentials=True,
        allow_methods=CORS_ALLOW_METHODS,
        allow_headers=CORS_ALLOW_HEADERS,
        expose_headers=CORS_EXPOSE_HEADERS,
    )
    app.add_middleware(IdempotencyMiddleware)
    app.add_middleware(RateLimitHeadersMiddleware)
    app.add_middleware(MetricsMiddleware, metrics=app.state.metrics)
    app.add_middleware(RequestContextMiddleware)
    install_handlers(app)
    install_idempotency(app)
    for router in routers:
        app.include_router(router)
    app.openapi = partial(build_openapi, app, cfg)  # type: ignore[method-assign]
    app.state.internal_openapi = partial(build_internal_openapi, app, cfg)
    return app


app = create_app()
