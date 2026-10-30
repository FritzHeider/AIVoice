"""FakeElevenLabs control routes (specs/30 §3.3 Control API; Decision P03-T04 rules 4, 9, 10).

``GET /sim/elevenlabs/tts/requests``; ``PUT``/``GET``/``DELETE /sim/elevenlabs/tts/fault``; ``PUT``/``GET
/sim/elevenlabs/api-keys``. Errors use the shared ``{"error": ...}`` control format.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, NonNegativeInt, PositiveInt, StringConstraints
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from va_sim.control import json_body
from va_sim.elevenlabs.store import FaultKind

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["ApiKeysBody", "FaultBody", "elevenlabs_control_routes"]


class FaultBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: FaultKind
    after_chunks: NonNegativeInt = 0
    delay_ms: NonNegativeInt = 0
    times: PositiveInt = 1


class ApiKeysBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    keys: list[Annotated[str, StringConstraints(min_length=1)]]

    def cleaned(self) -> tuple[str, ...]:
        """The keys without duplicates, in first-occurrence order."""
        return tuple(dict.fromkeys(self.keys))


def elevenlabs_control_routes(state: SimState) -> list[BaseRoute]:
    store = state.elevenlabs

    async def requests(_request: Request) -> Response:
        return JSONResponse({"requests": [record.to_json() for record in store.tts_requests]})

    async def put_fault(request: Request) -> Response:
        body = await json_body(request, FaultBody, allow_empty=False)
        if isinstance(body, JSONResponse):
            return body
        store.arm_fault(body.kind, after_chunks=body.after_chunks, delay_ms=body.delay_ms, times=body.times)
        return Response(status_code=204)

    async def get_fault(_request: Request) -> Response:
        return JSONResponse({"fault": store.fault.to_json() if store.fault is not None else None})

    async def delete_fault(_request: Request) -> Response:
        store.fault = None
        return Response(status_code=204)

    async def put_keys(request: Request) -> Response:
        body = await json_body(request, ApiKeysBody, allow_empty=False)
        if isinstance(body, JSONResponse):
            return body
        store.api_keys = body.cleaned()
        return Response(status_code=204)

    async def get_keys(_request: Request) -> Response:
        return JSONResponse({"keys": list(store.api_keys)})

    return [
        Route("/sim/elevenlabs/tts/requests", requests, methods=["GET"]),
        Route("/sim/elevenlabs/tts/fault", put_fault, methods=["PUT"]),
        Route("/sim/elevenlabs/tts/fault", get_fault, methods=["GET"]),
        Route("/sim/elevenlabs/tts/fault", delete_fault, methods=["DELETE"]),
        Route("/sim/elevenlabs/api-keys", put_keys, methods=["PUT"]),
        Route("/sim/elevenlabs/api-keys", get_keys, methods=["GET"]),
    ]
