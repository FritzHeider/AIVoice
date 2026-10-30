"""FakeTelnyx media control API for tests (specs/30-testing-and-evals.md §3.2 Control API, Decision 2026-10-29
P03-T03b rules 8 and 9).

``POST /sim/telnyx/calls/{ccid}/say`` makes the caller speak (synthetic marker audio, 202
``{utterance_id, duration_ms}``), ``POST .../dtmf`` sends media ``dtmf`` events, ``POST .../media/inject``
sends a raw frame on the media WebSocket, and ``GET .../agent_audio.wav`` returns the agent track (8 kHz mono
PCM16). The first three need a connected media session (409 ``not_streaming``) on a call that has not ended
(409 ``call_ended``).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from va_sim.audio.synth import synthesize_wire
from va_sim.control import json_body, sim_error
from va_sim.telnyx.call_control import find_call
from va_sim.telnyx.call_media import RECORDING_SAMPLE_RATE
from va_sim.telnyx.call_models import CallerDtmf, CallerSay, InjectFrame
from va_sim.telnyx.calls import Call
from va_sim.telnyx.media_session import MediaSession
from va_sim.wav import pcm16_wav

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["media_control_routes"]


def _live_session(call: Call) -> MediaSession | JSONResponse:
    if call.ended:
        return sim_error(409, "call_ended", f"call {call.call_control_id!r} has already ended")
    session = call.stream.session if call.stream is not None else None
    if session is None or not session.connected:
        return sim_error(409, "not_streaming", f"call {call.call_control_id!r} has no connected media stream")
    return session


def media_control_routes(state: SimState) -> list[BaseRoute]:
    def session_of(request: Request) -> MediaSession | JSONResponse:
        call = find_call(state, request)
        return call if isinstance(call, JSONResponse) else _live_session(call)

    async def say(request: Request) -> Response:
        session = session_of(request)
        if isinstance(session, JSONResponse):
            return session
        body = await json_body(request, CallerSay, allow_empty=False)
        if isinstance(body, JSONResponse):
            return body
        try:
            utterance_id = state.register_utterance(body.text, body.language, tuple(body.text.split()))
        except ValueError as exc:
            return sim_error(409, "utterance_limit", str(exc))
        wire = await asyncio.to_thread(
            synthesize_wire,
            utterance_id,
            body.text,
            sample_rate=session.fmt.sample_rate,
            seed=state.utterance_seed(utterance_id),
        )
        # The call may have ended or the stream stopped while the audio was synthesized.
        current = session_of(request)
        if current is not session:
            return current if isinstance(current, JSONResponse) else _not_streaming(request)
        session.queue_caller(wire, (utterance_id, body.text))
        duration_ms = len(wire) // session.fmt.bytes_per_ms
        return JSONResponse({"utterance_id": utterance_id, "duration_ms": duration_ms}, status_code=202)

    async def dtmf(request: Request) -> Response:
        session = session_of(request)
        if isinstance(session, JSONResponse):
            return session
        body = await json_body(request, CallerDtmf, allow_empty=False)
        if isinstance(body, JSONResponse):
            return body
        session.queue_dtmf(body.digits)
        return JSONResponse({"digits": body.digits}, status_code=202)

    async def inject(request: Request) -> Response:
        session = session_of(request)
        if isinstance(session, JSONResponse):
            return session
        body = await json_body(request, InjectFrame, allow_empty=False)
        if isinstance(body, JSONResponse):
            return body
        session.inject(dict(body.frame))
        return JSONResponse({"queued": True}, status_code=202)

    async def agent_audio(request: Request) -> Response:
        call = find_call(state, request)
        if isinstance(call, JSONResponse):
            return call
        wav = await asyncio.to_thread(pcm16_wav, [bytes(call.audio.agent)], RECORDING_SAMPLE_RATE)
        return Response(wav, media_type="audio/wav")

    return [
        Route("/sim/telnyx/calls/{call_control_id}/say", say, methods=["POST"]),
        Route("/sim/telnyx/calls/{call_control_id}/dtmf", dtmf, methods=["POST"]),
        Route("/sim/telnyx/calls/{call_control_id}/media/inject", inject, methods=["POST"]),
        Route("/sim/telnyx/calls/{call_control_id}/agent_audio.wav", agent_audio, methods=["GET"]),
    ]


def _not_streaming(request: Request) -> JSONResponse:
    call_control_id = request.path_params["call_control_id"]
    return sim_error(409, "not_streaming", f"call {call_control_id!r} has no connected media stream")
