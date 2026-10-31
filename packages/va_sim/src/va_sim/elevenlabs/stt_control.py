"""FakeElevenLabs STT control routes (specs/30 §3.3 Control API; Decisions P03-T05a rules 7-9, P03-T05b rules
9-10).

``GET /sim/elevenlabs/stt/sessions``; ``PUT``/``GET``/``DELETE /sim/elevenlabs/stt/fault``;
``POST``/``GET``/``DELETE /sim/elevenlabs/stt/transcripts``; ``GET /sim/elevenlabs/stt/batch``;
``PUT``/``GET``/``DELETE /sim/elevenlabs/stt/batch/fault``. Errors use the shared ``{"error": ...}`` control
format.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt, StringConstraints
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from va_sim.control import fault_route, json_body
from va_sim.elevenlabs.stt_store import BatchFaultKind, SttFaultKind

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["BatchFaultBody", "SttFaultBody", "TranscriptsBody", "stt_control_routes"]


class SttFaultBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SttFaultKind
    after_ms: NonNegativeInt = 0
    times: PositiveInt = 1


class BatchFaultBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: BatchFaultKind
    delay_ms: NonNegativeInt = 0
    times: PositiveInt = 1


class TranscriptsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    texts: Annotated[list[Annotated[str, StringConstraints(min_length=1)]], Field(min_length=1)]


def stt_control_routes(state: SimState) -> list[BaseRoute]:
    store = state.elevenlabs.stt

    async def sessions(_request: Request) -> Response:
        return JSONResponse({"sessions": [record.to_json() for record in store.sessions]})

    def arm_stt(body: SttFaultBody) -> None:
        store.arm_fault(body.kind, after_ms=body.after_ms, times=body.times)

    def clear_stt() -> None:
        store.fault = None

    def arm_batch(body: BatchFaultBody) -> None:
        store.arm_batch_fault(body.kind, delay_ms=body.delay_ms, times=body.times)

    def clear_batch() -> None:
        store.batch_fault = None

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

    async def batch(_request: Request) -> Response:
        return JSONResponse({"requests": [record.to_json() for record in store.batch]})

    return [
        Route("/sim/elevenlabs/stt/sessions", sessions, methods=["GET"]),
        fault_route(
            "/sim/elevenlabs/stt/fault",
            SttFaultBody,
            armed=lambda: None if store.fault is None else store.fault.to_json(),
            arm=arm_stt,
            clear=clear_stt,
        ),
        Route("/sim/elevenlabs/stt/transcripts", post_transcripts, methods=["POST"]),
        Route("/sim/elevenlabs/stt/transcripts", get_transcripts, methods=["GET"]),
        Route("/sim/elevenlabs/stt/transcripts", delete_transcripts, methods=["DELETE"]),
        Route("/sim/elevenlabs/stt/batch", batch, methods=["GET"]),
        fault_route(
            "/sim/elevenlabs/stt/batch/fault",
            BatchFaultBody,
            armed=lambda: None if store.batch_fault is None else store.batch_fault.to_json(),
            arm=arm_batch,
            clear=clear_batch,
        ),
    ]
