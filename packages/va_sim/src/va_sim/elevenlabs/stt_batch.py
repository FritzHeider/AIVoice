"""FakeElevenLabs batch transcription ``POST /v1/speech-to-text`` (specs/30 §3.3, specs/08 §8; Decision
P03-T05b).

Order of checks: ``xi-api-key`` (401), the armed batch fault (500 / 429 / delay), the multipart form (422),
the audio source (400), then analysis. The source is the uploaded ``file`` or a URL: a
``/sim/recordings/<id>.wav`` path of a stored recording is read in-process whatever the host, any other URL is
fetched (10 s timeout, no redirects, 100 MB cap). Analysis runs in a worker thread under ``hold_clock`` on
snapshots of the utterance registry and the TTS records; successful requests are recorded.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

import httpx
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from va_sim.elevenlabs.auth import API_KEY_HEADER, detail_error, rest_auth_error
from va_sim.elevenlabs.rest_io import error_item, unprocessable
from va_sim.elevenlabs.stt_batch_audio import (
    BatchAudio,
    BatchAudioError,
    BatchSegment,
    read_batch_audio,
    transcribe,
)
from va_sim.elevenlabs.stt_batch_json import batch_response
from va_sim.elevenlabs.stt_batch_params import BatchParams, parse_batch_form
from va_sim.elevenlabs.stt_store import SttBatchRecord
from va_sim.tasks import hold_clock

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["FETCH_TIMEOUT_S", "MAX_UPLOAD_BYTES", "batch_routes"]

MAX_UPLOAD_BYTES: Final = 100 * 1024 * 1024
FETCH_TIMEOUT_S: Final = 10.0
_MAX_FIELDS: Final = 2000
_MULTIPART: Final = "multipart/form-data"
_RECORDING_PATH: Final = re.compile(r"^/sim/recordings/(?P<id>[^/]+)\.wav$")
_MS_PER_S: Final = 1000


def _too_large() -> BatchAudioError:
    return BatchAudioError(
        "file_too_large", f"The audio must be at most {MAX_UPLOAD_BYTES // (1024 * 1024)} MB"
    )


async def _fetch(url: str) -> bytes:
    """The body behind ``url``; :class:`BatchAudioError` ``invalid_source_url`` / ``file_too_large``."""
    try:
        async with (
            httpx.AsyncClient(timeout=FETCH_TIMEOUT_S, follow_redirects=False) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != httpx.codes.OK:
                why = f"HTTP {response.status_code}"
            else:
                body = bytearray()
                async for part in response.aiter_bytes():
                    body.extend(part)
                    if len(body) > MAX_UPLOAD_BYTES:
                        raise _too_large()
                return bytes(body)
    except httpx.TimeoutException:
        why = "timed out"
    except httpx.HTTPError as exc:
        why = type(exc).__name__
    raise BatchAudioError("invalid_source_url", f"Could not fetch audio from {url} ({why})")


class _BatchRequest:
    """One authenticated batch request against ``state``."""

    def __init__(self, state: SimState, request: Request) -> None:
        self.state = state
        self.request = request
        self.received_ms = state.clock.virtual_ms()

    async def fault(self) -> Response | None:
        """Apply the armed batch fault: its response, or ``None`` (none armed, or ``delay`` waited)."""
        fault = self.state.elevenlabs.stt.take_batch_fault()
        if fault is None:
            return None
        if fault.kind == "error":
            return detail_error(500, "internal_error", "Simulated batch STT failure")
        if fault.kind == "rate_limited":
            return detail_error(429, "too_many_concurrent_requests", "Simulated batch STT rate limit")
        await self.state.clock.sleep(fault.delay_ms / _MS_PER_S)
        return None

    async def source(self, params: BatchParams) -> bytes:
        """The audio bytes of the upload or the URL; :class:`BatchAudioError` when unusable."""
        upload = params.upload
        if upload is not None:
            if upload.size is not None and upload.size > MAX_UPLOAD_BYTES:
                raise _too_large()
            return await upload.read()
        url = params.url or ""
        match = _RECORDING_PATH.match(urlsplit(url).path)
        stored = self.state.recordings.get(match["id"]) if match is not None else None
        return stored if stored is not None else await _fetch(url)

    async def transcribe(self, params: BatchParams, data: bytes) -> Response:
        utterances = {key: (value.text, value.language) for key, value in self.state.utterances.items()}
        generated = tuple(record.generated_text for record in self.state.elevenlabs.tts_requests)
        form = params.form

        def work() -> tuple[BatchAudio, list[list[BatchSegment]]]:
            audio = read_batch_audio(data, form.file_format, multi_channel=form.use_multi_channel)
            return audio, transcribe(audio, utterances, generated)

        try:
            async with hold_clock(self.state.clock, self.state.tasks, name="stt-batch"):
                audio, channels = await asyncio.to_thread(work)
        except BatchAudioError as exc:
            return detail_error(400, exc.status, exc.message)
        transcription_id = self.state.ids.uuid().replace("-", "")
        body = batch_response(channels, form, transcription_id=transcription_id, duration_s=audio.duration_s)
        self.record(params, transcription_id, audio)
        return JSONResponse(body)

    def record(self, params: BatchParams, transcription_id: str, audio: BatchAudio) -> None:
        form = params.form
        url = params.url
        self.state.elevenlabs.stt.batch.append(
            SttBatchRecord(
                transcription_id=transcription_id,
                model_id=form.model_id,
                source="file" if url is None else "url",
                source_url=url,
                file_format=form.file_format,
                channels=len(audio.channels),
                use_multi_channel=form.use_multi_channel,
                multichannel_output_style=form.multichannel_output_style,
                diarize=form.diarize,
                num_speakers=form.num_speakers,
                timestamps_granularity=form.timestamps_granularity,
                language_code=form.language_code,
                keyterms=list(form.keyterms),
                entity_detection=list(form.entity_detection),
                enable_logging=params.enable_logging,
                audio_duration_secs=round(audio.duration_s, 3),
                received_ms=self.received_ms,
            )
        )

    async def run(self) -> Response:
        failed = await self.fault()
        if failed is not None:
            return failed
        content_type = self.request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != _MULTIPART:
            return unprocessable(
                [error_item(["body"], "the body must be multipart/form-data", "value_error")]
            )
        try:
            async with self.request.form(max_files=1, max_fields=_MAX_FIELDS) as form:
                params = parse_batch_form(form, self.request.query_params)
                if isinstance(params, list):
                    return unprocessable(params)
                data = await self.source(params)
        except (HTTPException, MultiPartException) as exc:
            message = exc.detail if isinstance(exc, HTTPException) else exc.message
            return unprocessable([error_item(["body"], str(message), "value_error")])
        except BatchAudioError as exc:
            return detail_error(400, exc.status, exc.message)
        return await self.transcribe(params, data)


def batch_routes(state: SimState) -> list[BaseRoute]:
    async def speech_to_text(request: Request) -> Response:
        key = request.headers.get(API_KEY_HEADER)
        denied = rest_auth_error(key, state.elevenlabs.accepts(key))
        if denied is not None:
            return denied
        return await _BatchRequest(state, request).run()

    return [Route("/v1/speech-to-text", speech_to_text, methods=["POST"])]
