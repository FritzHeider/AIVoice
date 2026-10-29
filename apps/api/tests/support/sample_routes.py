"""Test-only routers exercising every error path in both envelope families."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from va_api.envelopes import envelope_router, legacy_router
from va_common.errors import AppError

LEGACY_PREFIX = "/v1/calls/_t"
ENVELOPE_PREFIX = "/v1/knowledge/_t"


class Voicemail(BaseModel):
    action: Literal["hangup", "leave_message"]


class SampleBody(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"phone_number": "+14155550123", "voicemail": {"action": "hangup"}}]}
    )

    phone_number: str = Field(pattern=r"^\+")
    voicemail: Voicemail

    @field_validator("phone_number")
    @classmethod
    def _e164(cls, value: str) -> str:
        if not value[1:].isdigit():
            msg = "must be E.164"
            raise ValueError(msg)
        return value


class SampleOk(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"status": "success", "limit": 25}]})

    status: Literal["success"]
    limit: int


def _routes(router: APIRouter, family: str) -> APIRouter:
    @router.get(
        "/app_error", operation_id=f"sample_{family}_app_error", openapi_extra={"x-scope": "calls:read"}
    )
    async def app_error() -> SampleOk:
        raise AppError(
            "NUMBER_BLOCKED", "This number is blocked.", field="phone_number", details={"reason": "dnc"}
        )

    @router.get(
        "/kb_error", operation_id=f"sample_{family}_kb_error", openapi_extra={"x-scope": "calls:read"}
    )
    async def kb_error() -> SampleOk:
        raise AppError("KB_ERROR", "Embedding failed.", http_status=500)

    @router.get("/locked", operation_id=f"sample_{family}_locked", openapi_extra={"x-scope": "calls:read"})
    async def locked() -> SampleOk:
        raise AppError("AUTH_LOCKED", "Locked.", headers={"Retry-After": "42"})

    @router.post("/body", operation_id=f"sample_{family}_body", openapi_extra={"x-scope": "calls:write"})
    async def body(payload: SampleBody) -> SampleOk:
        return SampleOk(status="success", limit=len(payload.phone_number))

    @router.get("/query", operation_id=f"sample_{family}_query", openapi_extra={"x-scope": "calls:read"})
    async def query(limit: int = Query(25, ge=1, le=100)) -> SampleOk:
        return SampleOk(status="success", limit=limit)

    @router.get(
        "/http/{status}", operation_id=f"sample_{family}_http", openapi_extra={"x-scope": "calls:read"}
    )
    async def http(status: int) -> SampleOk:
        headers = {"Retry-After": "7"} if status == 429 else None
        raise HTTPException(status_code=status, detail=f"detail {status}", headers=headers)

    @router.get("/boom", operation_id=f"sample_{family}_boom", openapi_extra={"x-scope": "calls:read"})
    async def boom() -> SampleOk:
        msg = "secret detail"
        raise RuntimeError(msg)

    return router


legacy_sample = _routes(legacy_router(prefix=LEGACY_PREFIX, tags=["Calls"]), "legacy")
envelope_sample = _routes(envelope_router(prefix=ENVELOPE_PREFIX, tags=["Knowledge"]), "envelope")
