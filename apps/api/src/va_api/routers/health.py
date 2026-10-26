"""Health endpoints: public ``GET /v1/health`` and the unlisted probes (specs/27-observability.md §9)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from va_api import __version__
from va_api.envelopes import legacy_router
from va_common.health import liveness_body, run_checks
from va_common.metrics import render_latest

__all__ = ["HealthStatus", "probe_router", "public_router"]

public_router = legacy_router(tags=["Health"])
probe_router = APIRouter(include_in_schema=False)


class HealthStatus(BaseModel):
    """Public health status; never reveals dependency state."""

    model_config = ConfigDict(title="HealthStatus", json_schema_extra={"examples": [{"status": "ok"}]})

    status: Literal["ok"]


@public_router.get(
    "/v1/health",
    operation_id="health_get",
    response_model=HealthStatus,
    summary="Service health",
    openapi_extra={"x-scope": "public"},
)
async def health_get() -> HealthStatus:
    return HealthStatus(status="ok")


@probe_router.get("/healthz")
async def healthz() -> dict[str, str]:
    return liveness_body(__version__)


@probe_router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    state = request.app.state
    report = await run_checks(
        state.container.readiness_checks(), timeout_s=state.readiness_timeout_s, version=__version__
    )
    return JSONResponse(report.body(), status_code=report.http_status)


@probe_router.get("/metrics")
async def metrics(request: Request) -> Response:
    body, content_type = render_latest(request.app.state.metrics.registry)
    return Response(content=body, media_type=content_type)
