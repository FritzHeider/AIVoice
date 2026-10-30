"""FakeElevenLabs REST synthesis (specs/30 §3.3, specs/09 §9; Decision P03-T04 rule 11).

``POST /v1/text-to-speech/{voice_id}`` (whole audio), ``.../stream`` (the 200 ms pieces, first after
``tts_ttfa_ms``), and ``.../with-timestamps`` (``audio_base64`` plus absolute alignment in seconds). Same
audio generator as the WebSocket. Errors follow ElevenLabs: 401 ``{"detail": {"status", "message"}}`` and
422 ``{"detail": [{"loc", "msg", "type"}]}``; an armed ``error`` fault answers 500, ``delay`` adds latency.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

import orjson
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Route

from va_sim.elevenlabs.auth import API_KEY_HEADER, detail_error, rest_auth_error
from va_sim.elevenlabs.rest_io import error_item, unprocessable, validation_errors
from va_sim.elevenlabs.store import FaultKind, TtsRequest
from va_sim.elevenlabs.tts_audio import (
    OutputFormat,
    TtsChunk,
    absolute_alignment,
    chunks,
    parse_output_format,
    synthesize,
)
from va_sim.elevenlabs.tts_params import DEFAULT_OUTPUT_FORMAT
from va_sim.tasks import hold_clock

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["DEFAULT_REST_MODEL_ID", "TtsBody", "tts_rest_routes"]

DEFAULT_REST_MODEL_ID: Final = "eleven_multilingual_v2"
_MAX_TEXT: Final = 40_000
_REST_FAULTS: Final[frozenset[FaultKind]] = frozenset({"error", "delay"})
_MS_PER_S: Final = 1000

type Mode = Literal["convert", "stream", "timestamps"]


class TtsBody(BaseModel):
    """The JSON body of a REST synthesis request (unknown fields ignored)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    text: str = Field(min_length=1, max_length=_MAX_TEXT)
    model_id: str = Field(default=DEFAULT_REST_MODEL_ID, min_length=1)
    voice_settings: dict[str, Any] | None = None
    language_code: str | None = None
    apply_text_normalization: Literal["auto", "on", "off"] = "auto"


@dataclass(frozen=True, slots=True)
class _Accepted:
    body: TtsBody
    fmt: OutputFormat


async def _accept(state: SimState, request: Request) -> _Accepted | Response:
    key = request.headers.get(API_KEY_HEADER)
    denied = rest_auth_error(key, state.elevenlabs.accepts(key))
    if denied is not None:
        return denied
    try:
        fmt = parse_output_format(request.query_params.get("output_format", DEFAULT_OUTPUT_FORMAT))
    except ValueError as exc:
        return unprocessable([error_item(["query", "output_format"], str(exc), "enum")])
    raw = await request.body()
    try:
        payload = orjson.loads(raw)
    except orjson.JSONDecodeError as exc:
        return unprocessable([error_item(["body"], str(exc), "json_invalid")])
    try:
        body = TtsBody.model_validate(payload)
    except ValidationError as exc:
        return unprocessable(validation_errors(exc))
    return _Accepted(body=body, fmt=fmt)


def _headers(record: TtsRequest) -> dict[str, str]:
    return {"x-character-count": str(record.characters), "request-id": record.id}


class _Synthesis:
    """One accepted REST request: fault handling, the record, synthesis off the loop, and latency."""

    def __init__(self, state: SimState, voice_id: str, accepted: _Accepted) -> None:
        self.state = state
        self.fmt = accepted.fmt
        self.text = accepted.body.text
        self.record = state.elevenlabs.new_request(
            transport="rest",
            voice_id=voice_id,
            model_id=accepted.body.model_id,
            output_format=accepted.fmt.name,
            context_id=None,
            started_ms=state.clock.virtual_ms(),
        )
        self.record.text = self.text
        self.record.generated_text = self.text
        self.record.characters = len(self.text)

    async def fault(self) -> Response | None:
        """Apply an armed fault: the 500 response for ``error``; ``delay`` waits and returns ``None``."""
        fault = self.state.elevenlabs.take_fault(_REST_FAULTS)
        if fault is None:
            return None
        if fault.kind == "delay":
            await self.state.clock.sleep(fault.delay_ms / _MS_PER_S)
            return None
        self.record.end("fault", self.state.clock.virtual_ms(), interrupted=True)
        return detail_error(500, "internal_error", "Simulated TTS failure")

    async def run[T](self, work: Callable[[str, OutputFormat], T]) -> T:
        async with hold_clock(self.state.clock, self.state.tasks, name=f"tts-rest-{self.record.id}"):
            return await asyncio.to_thread(work, self.text, self.fmt)

    async def first_audio_latency(self) -> None:
        await self.state.clock.sleep(self.state.latencies.tts_ttfa_ms / _MS_PER_S)

    def complete(self) -> None:
        self.record.end("completed", self.state.clock.virtual_ms())


async def _convert(synthesis: _Synthesis) -> Response:
    audio = await synthesis.run(synthesize)
    await synthesis.first_audio_latency()
    synthesis.complete()
    return Response(audio, media_type=synthesis.fmt.content_type, headers=_headers(synthesis.record))


async def _timestamps(synthesis: _Synthesis) -> Response:
    audio = await synthesis.run(synthesize)
    await synthesis.first_audio_latency()
    synthesis.complete()
    alignment = absolute_alignment(synthesis.text)
    body = {
        "audio_base64": base64.b64encode(audio).decode("ascii"),
        "alignment": alignment,
        "normalized_alignment": alignment,
    }
    return JSONResponse(body, headers=_headers(synthesis.record))


async def _stream(synthesis: _Synthesis) -> Response:
    pieces = await synthesis.run(chunks)

    async def body(items: list[TtsChunk]) -> AsyncIterator[bytes]:
        await synthesis.first_audio_latency()
        for item in items:
            yield item.audio
        synthesis.complete()

    return StreamingResponse(
        body(pieces), media_type=synthesis.fmt.content_type, headers=_headers(synthesis.record)
    )


_HANDLERS: Final[dict[Mode, Callable[[_Synthesis], Awaitable[Response]]]] = {
    "convert": _convert,
    "stream": _stream,
    "timestamps": _timestamps,
}


def tts_rest_routes(state: SimState) -> list[BaseRoute]:
    def endpoint(mode: Mode) -> Callable[[Request], Awaitable[Response]]:
        async def handle(request: Request) -> Response:
            accepted = await _accept(state, request)
            if isinstance(accepted, Response):
                return accepted
            synthesis = _Synthesis(state, request.path_params["voice_id"], accepted)
            failed = await synthesis.fault()
            if failed is not None:
                return failed
            return await _HANDLERS[mode](synthesis)

        return handle

    base = "/v1/text-to-speech/{voice_id}"
    return [
        Route(base, endpoint("convert"), methods=["POST"]),
        Route(f"{base}/stream", endpoint("stream"), methods=["POST"]),
        Route(f"{base}/with-timestamps", endpoint("timestamps"), methods=["POST"]),
    ]
