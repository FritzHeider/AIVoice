"""FakeElevenLabs voices control routes (specs/30 §3.3 Control API; Decision P03-T05c rules 11-12).

``GET /sim/elevenlabs/voices``; ``GET /sim/elevenlabs/voices/requests``; ``PUT``/``GET``/``DELETE
/sim/elevenlabs/voices/fault``; ``GET``/``PATCH /sim/elevenlabs/subscription``; ``GET
/sim/elevenlabs/previews/{voice_id}.mp3`` (the ``preview_url`` of premade voices). No authentication; errors
use the shared ``{"error": ...}`` control format.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, NonNegativeInt, StringConstraints
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from va_sim.control import fault_route, json_body, sim_error
from va_sim.elevenlabs.stt_control import BatchFaultBody
from va_sim.elevenlabs.tts_audio import parse_output_format, synthesize
from va_sim.elevenlabs.voice_json import subscription_json, voice_json
from va_sim.tasks import hold_clock

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["SubscriptionBody", "voice_control_routes"]

_PREVIEW_FORMAT = parse_output_format("mp3_44100_128")


class SubscriptionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tier: Annotated[str, StringConstraints(min_length=1)] | None = None
    character_limit: NonNegativeInt | None = None
    voice_limit: NonNegativeInt | None = None
    max_voice_add_edits: NonNegativeInt | None = None


def voice_control_routes(state: SimState) -> list[BaseRoute]:
    store = state.elevenlabs.voices

    def subscription() -> JSONResponse:
        return JSONResponse(subscription_json(state.elevenlabs, int(state.clock.now().timestamp())))

    async def voices(request: Request) -> Response:
        base = str(request.base_url)
        listing = [
            {
                **voice_json(record, base),
                "remove_background_noise": record.remove_background_noise,
                "edits": record.edits,
            }
            for record in store.ordered()
        ]
        return JSONResponse({"voices": listing})

    async def requests(_request: Request) -> Response:
        return JSONResponse({"requests": [record.to_json() for record in store.requests]})

    def arm(body: BatchFaultBody) -> None:
        store.arm_fault(body.kind, delay_ms=body.delay_ms, times=body.times)

    def clear() -> None:
        store.fault = None

    async def get_subscription(_request: Request) -> Response:
        return subscription()

    async def patch_subscription(request: Request) -> Response:
        body = await json_body(request, SubscriptionBody, allow_empty=False)
        if isinstance(body, JSONResponse):
            return body
        for name, value in body.model_dump(exclude_none=True).items():
            setattr(store.subscription, name, value)
        return subscription()

    async def preview(request: Request) -> Response:
        voice_id = request.path_params["voice_id"]
        record = store.get(voice_id)
        if record is None:
            return sim_error(404, "not_found", f"voice {voice_id} not found")
        text = f"Hi, this is {record.name}."
        async with hold_clock(state.clock, state.tasks, name="voice-preview"):
            audio = await asyncio.to_thread(synthesize, text, _PREVIEW_FORMAT)
        return Response(audio, media_type=_PREVIEW_FORMAT.content_type)

    return [
        Route("/sim/elevenlabs/voices", voices, methods=["GET"]),
        Route("/sim/elevenlabs/voices/requests", requests, methods=["GET"]),
        fault_route(
            "/sim/elevenlabs/voices/fault",
            BatchFaultBody,
            armed=lambda: None if store.fault is None else store.fault.to_json(),
            arm=arm,
            clear=clear,
        ),
        Route("/sim/elevenlabs/subscription", get_subscription, methods=["GET"]),
        Route("/sim/elevenlabs/subscription", patch_subscription, methods=["PATCH"]),
        Route("/sim/elevenlabs/previews/{voice_id}.mp3", preview, methods=["GET"]),
    ]
