"""Idempotency test routers (both envelope families) and the container that counts their runs."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field

from auth_helpers import AuthContainer
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from va_api.auth import rate_limit, require_scope
from va_api.envelopes import envelope_router, legacy_router

__all__ = ["IDEM_ENVELOPE_PREFIX", "IDEM_LEGACY_PREFIX", "IdempotencyContainer", "idempotency_app"]

IDEM_LEGACY_PREFIX = "/v1/calls/_idem"
IDEM_ENVELOPE_PREFIX = "/v1/knowledge/_idem"


@dataclass
class IdempotencyContainer(AuthContainer):
    """The auth container plus run counters per route and a gate holding ``/gate`` until opened."""

    hits: Counter[str] = field(default_factory=Counter)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    gate: asyncio.Event = field(default_factory=asyncio.Event)


class EchoBody(BaseModel):
    name: str
    count: int


def _container(request: Request) -> IdempotencyContainer:
    container: IdempotencyContainer = request.app.state.container
    return container


def _idempotency_routes(router: APIRouter, family: str) -> APIRouter:
    write = [Depends(require_scope("calls:write"))]

    @router.post("/status/{code}", operation_id=f"idem_{family}_status", dependencies=write)
    async def status(code: int, request: Request) -> JSONResponse:
        container = _container(request)
        container.hits["status"] += 1
        return JSONResponse({"n": container.hits["status"], "code": code}, status_code=code)

    @router.post("/boom", operation_id=f"idem_{family}_boom", dependencies=write)
    async def boom(request: Request) -> None:
        _container(request).hits["boom"] += 1
        msg = "endpoint failed"
        raise RuntimeError(msg)

    @router.post("/text", operation_id=f"idem_{family}_text", dependencies=write)
    async def text(request: Request) -> PlainTextResponse:
        _container(request).hits["text"] += 1
        return PlainTextResponse("ok")

    @router.post("/echo", operation_id=f"idem_{family}_echo", dependencies=write)
    async def echo(body: EchoBody, request: Request) -> dict[str, object]:
        container = _container(request)
        container.hits["echo"] += 1
        return {"name": body.name, "count": body.count, "n": container.hits["echo"]}

    @router.post("/gate", operation_id=f"idem_{family}_gate", dependencies=write)
    async def gate(request: Request) -> dict[str, int]:
        container = _container(request)
        container.hits["gate"] += 1
        container.entered.set()
        await container.gate.wait()
        return {"n": container.hits["gate"]}

    @router.post(
        "/send",
        operation_id=f"idem_{family}_send",
        dependencies=[*write, Depends(rate_limit("calls_send"))],
    )
    async def send(request: Request) -> dict[str, int]:
        container = _container(request)
        container.hits["send"] += 1
        return {"n": container.hits["send"]}

    return router


idem_legacy = _idempotency_routes(legacy_router(prefix=IDEM_LEGACY_PREFIX, tags=["Calls"]), "legacy")
idem_envelope = _idempotency_routes(
    envelope_router(prefix=IDEM_ENVELOPE_PREFIX, tags=["Knowledge"]), "envelope"
)


def idempotency_app(container: IdempotencyContainer) -> FastAPI:
    """The production app plus the idempotency test routers, served by ``container``."""
    from api_helpers import make_settings  # noqa: PLC0415 - mirrors auth_helpers.auth_app

    from va_api.main import ROUTERS, create_app  # noqa: PLC0415

    return create_app(
        make_settings(),
        container_factory=container,
        init_observability=False,
        routers=(*ROUTERS, idem_legacy, idem_envelope),
    )
