"""Health endpoints: public ``GET /v1/health`` and the unlisted probes (specs/27-observability.md §9)."""

from __future__ import annotations

from typing import Final, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict

from va_api.envelopes import legacy_router
from va_common.service import ProbeState, liveness_response, metrics_response, readiness_response

__all__ = ["HealthStatus", "probe_router", "public_router"]

public_router = legacy_router(tags=["Health"])
# Opted out of the cross-tenant suite (specs/04 §2.4): unauthenticated probes of the pod.
probe_router = APIRouter(include_in_schema=False)
_PROBE: Final = {"x-public": "probe"}


class HealthStatus(BaseModel):
    """Public health status; never reveals dependency state."""

    model_config = ConfigDict(title="HealthStatus", json_schema_extra={"examples": [{"status": "ok"}]})

    status: Literal["ok"]


@public_router.get(
    "/v1/health",
    operation_id="health_get",
    response_model=HealthStatus,
    summary="Service health",
    openapi_extra={"x-scope": "public", "x-public": "health"},
)
async def health_get() -> HealthStatus:
    return HealthStatus(status="ok")


def _probes(request: Request) -> ProbeState:
    probes: ProbeState = request.app.state.probes
    return probes


@probe_router.get("/healthz", openapi_extra=_PROBE)
async def healthz(request: Request) -> Response:
    return liveness_response(_probes(request))


@probe_router.get("/readyz", openapi_extra=_PROBE)
async def readyz(request: Request) -> Response:
    return await readiness_response(_probes(request))


@probe_router.get("/metrics", openapi_extra=_PROBE)
async def metrics(request: Request) -> Response:
    return metrics_response(_probes(request))
