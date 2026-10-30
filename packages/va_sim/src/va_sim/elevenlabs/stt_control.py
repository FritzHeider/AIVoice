"""FakeElevenLabs realtime STT control routes (specs/30 §3.3 Control API; Decision P03-T05a rules 7-9).

``GET /sim/elevenlabs/stt/sessions``; ``PUT``/``GET``/``DELETE /sim/elevenlabs/stt/fault``;
``POST``/``GET``/``DELETE /sim/elevenlabs/stt/transcripts``. Errors use the shared ``{"error": ...}`` control
format.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt, StringConstraints
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from va_sim.control import json_body
from va_sim.elevenlabs.stt_store import SttFaultKind

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["SttFaultBody", "TranscriptsBody", "stt_control_routes"]


class SttFaultBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SttFaultKind
    after_ms: NonNegativeInt = 0
    times: PositiveInt = 1


class TranscriptsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    texts: Annotated[list[Annotated[str, StringConstraints(min_length=1)]], Field(min_length=1)]


def stt_control_routes(state: SimState) -> list[BaseRoute]:
    store = state.elevenlabs.stt

    async def sessions(_request: Request) -> Response:
        return JSONResponse({"sessions": [record.to_json() for record in store.sessions]})

    async def fault(request: Request) -> Response:
        if request.method == "GET":
            armed = store.fault
            return JSONResponse({"fault": None if armed is None else armed.to_json()})
        if request.method == "DELETE":
            store.fault = None
            return Response(status_code=204)
        body = await json_body(request, SttFaultBody, allow_empty=False)
        if isinstance(body, JSONResponse):
            return body
        store.arm_fault(body.kind, after_ms=body.after_ms, times=body.times)
        return Response(status_code=204)

    async def post_transcripts(request: Request) -> Response:
        body = await json_body(request, TranscriptsBody, allow_empty=False)
        if isinstance(body, JSONResponse):
            return body
        store.script.extend(body.texts)
        return Response(status_code=204)

    async def get_transcripts(_request: Request) -> Response:
        return JSONResponse({"texts": list(store.script)})

    async def delete_transcripts(_request: Request) -> Response:
        store.script.clear()
        return Response(status_code=204)

    return [
        Route("/sim/elevenlabs/stt/sessions", sessions, methods=["GET"]),
        Route("/sim/elevenlabs/stt/fault", fault, methods=["PUT", "GET", "DELETE"]),
        Route("/sim/elevenlabs/stt/transcripts", post_transcripts, methods=["POST"]),
        Route("/sim/elevenlabs/stt/transcripts", get_transcripts, methods=["GET"]),
        Route("/sim/elevenlabs/stt/transcripts", delete_transcripts, methods=["DELETE"]),
    ]
