"""FakeElevenLabs batch STT test helpers: agent and caller tracks, track placement, WAV files, multipart
requests, and a tiny HTTP server for ``source_url`` fetches.

Tracks are int16 arrays; ``μ-law`` round trips mirror what a FakeTelnyx recording holds (the media stream is
PCMU). Multipart requests send every field as a form part (``(None, value)``) so URL-only requests are still
``multipart/form-data``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final

import httpx
import numpy as np
import uvicorn
from numpy.typing import NDArray
from starlette.applications import Starlette
from stt_helpers import caller_pcm

from va_common.service import ReadyServer, StopController, run_server
from va_sim.audio.codec import ulaw_decode, ulaw_encode
from va_sim.elevenlabs.tts_audio import pcm_samples
from va_sim.state import SimState
from va_sim.wav import pcm16_wav

__all__ = [
    "BATCH",
    "MODEL",
    "agent_track",
    "caller_track",
    "multipart",
    "place",
    "post_batch",
    "served_app",
    "wav_file",
    "word_entries",
]

BATCH: Final = "/v1/speech-to-text"
MODEL: Final = "scribe_v2"
_READY_TIMEOUT_S: Final = 10.0

type Fields = Mapping[str, str | Sequence[str]]
type Part = tuple[str, tuple[str | None, bytes | str] | tuple[str, bytes, str]]


def _round_trip(samples: NDArray[np.int16], ulaw: bool) -> NDArray[np.int16]:
    return ulaw_decode(ulaw_encode(samples)) if ulaw else samples


def agent_track(text: str, sample_rate: int = 8000, *, ulaw: bool = True) -> NDArray[np.int16]:
    """The FakeElevenLabs TTS audio of ``text`` (μ-law round-tripped at 8 kHz by default)."""
    return _round_trip(pcm_samples(text, sample_rate), ulaw and sample_rate == 8000)


def caller_track(
    state: SimState, text: str, sample_rate: int = 8000, *, language: str = "en", ulaw: bool = True
) -> NDArray[np.int16]:
    """A registered caller utterance of ``text`` (μ-law round-tripped at 8 kHz by default)."""
    return _round_trip(caller_pcm(state, text, sample_rate, language), ulaw and sample_rate == 8000)


def place(
    tracks: Sequence[tuple[int, NDArray[np.int16]]], length_ms: int, sample_rate: int = 8000
) -> NDArray[np.int16]:
    """``length_ms`` of silence with each ``(offset_ms, samples)`` track added at its offset."""
    out = np.zeros(sample_rate * length_ms // 1000, dtype=np.int32)
    for offset_ms, samples in tracks:
        start = sample_rate * offset_ms // 1000
        out[start : start + samples.size] += samples
    clipped: NDArray[np.int16] = np.clip(out, -32_768, 32_767).astype(np.int16)
    return clipped


def wav_file(channels: Sequence[NDArray[np.int16]], sample_rate: int = 8000) -> bytes:
    return pcm16_wav([channel.astype("<i2").tobytes() for channel in channels], sample_rate)


def multipart(fields: Fields, file: bytes | None = None) -> list[Part]:
    """httpx ``files`` parts: every field (repeated for sequences) and the optional upload."""
    parts: list[Part] = []
    for name, value in fields.items():
        values = [value] if isinstance(value, str) else list(value)
        parts.extend((name, (None, item)) for item in values)
    if file is not None:
        parts.append(("file", ("audio.wav", file, "audio/wav")))
    return parts


async def post_batch(
    client: httpx.AsyncClient,
    fields: Fields,
    file: bytes | None = None,
    *,
    params: Mapping[str, str] | None = None,
) -> httpx.Response:
    """``POST /v1/speech-to-text`` with ``model_id`` defaulted."""
    return await client.post(BATCH, files=multipart({"model_id": MODEL, **fields}, file), params=params)


def word_entries(words: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in words if entry["type"] == "word"]


@asynccontextmanager
async def served_app(app: Starlette) -> AsyncIterator[str]:
    """``app`` on an ephemeral 127.0.0.1 port; yields its base URL."""
    stop = StopController()
    ready = asyncio.Event()
    servers: list[ReadyServer] = []

    def factory(config: uvicorn.Config) -> ReadyServer:
        servers.append(ReadyServer(config, ready=ready))
        return servers[0]

    task = asyncio.create_task(
        run_server(app, host="127.0.0.1", port=0, stop=stop, install_signals=False, server_factory=factory)
    )
    try:
        async with asyncio.timeout(_READY_TIMEOUT_S):
            await ready.wait()
        yield servers[0].url
    finally:
        stop.request("served_app.exit")
        await task
