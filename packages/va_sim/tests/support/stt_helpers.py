"""FakeElevenLabs realtime STT test helpers: client socket, synthesized caller audio, chunking, readers.

Socket tests reuse ``tts_helpers`` (``served``, ``realtime_state``, ``send``, ``recv_json``, ``wait_closed``)
and run on the wall clock. Audio helpers return wire bytes in the session's ``audio_format``.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlencode

import numpy as np
from numpy.typing import NDArray
from tts_helpers import API_KEY, recv_json, send
from websockets.asyncio.client import ClientConnection, connect

from va_sim.audio import synthesize_pcm16, ulaw_encode
from va_sim.elevenlabs.stt_params import AUDIO_FORMATS, SttFormat
from va_sim.state import SimState

__all__ = [
    "ULAW",
    "caller_audio",
    "caller_pcm",
    "chunked",
    "encode",
    "recv_type",
    "send_audio",
    "silence",
    "stt_socket",
    "stt_url",
]

ULAW = {"model_id": "scribe_v2_realtime", "audio_format": "ulaw_8000"}

type Frame = dict[str, Any]


def stt_url(base: str, query: Mapping[str, str | Sequence[str]] | None = None) -> str:
    suffix = f"?{urlencode(dict(query), doseq=True)}" if query else ""
    return f"{base}/v1/speech-to-text/realtime{suffix}"


@asynccontextmanager
async def stt_socket(
    base: str, query: Mapping[str, str | Sequence[str]] | None = None, *, key: str | None = API_KEY
) -> AsyncIterator[ClientConnection]:
    """A client socket (``xi-api-key`` header unless ``key`` is None); ``query`` defaults to μ-law 8 kHz."""
    headers = {"xi-api-key": key} if key is not None else {}
    async with connect(
        stt_url(base, ULAW if query is None else query), additional_headers=headers, ping_interval=None
    ) as ws:
        yield ws


def caller_pcm(state: SimState, text: str, sample_rate: int, language: str = "en") -> NDArray[np.int16]:
    """Register ``text`` as an utterance and synthesize its caller speech (marker + body) at the rate."""
    utterance_id = state.register_utterance(text, language, tuple(text.split()))
    return synthesize_pcm16(
        utterance_id, text, sample_rate=sample_rate, seed=state.utterance_seed(utterance_id)
    )


def encode(pcm: NDArray[np.int16], fmt: SttFormat) -> bytes:
    """Wire bytes of ``pcm`` in ``fmt`` (μ-law, or little-endian PCM16)."""
    if fmt.encoding == "ulaw":
        return ulaw_encode(pcm)
    return pcm.astype("<i2").tobytes()


def silence(fmt: SttFormat, ms: int) -> NDArray[np.int16]:
    return np.zeros(fmt.sample_rate * ms // 1000, dtype=np.int16)


def caller_audio(
    state: SimState, text: str, audio_format: str, *, lead_ms: int = 200, tail_ms: int = 400
) -> bytes:
    """Wire bytes: ``lead_ms`` silence, the registered utterance, ``tail_ms`` silence."""
    fmt = AUDIO_FORMATS[audio_format]
    pcm = np.concatenate(
        [silence(fmt, lead_ms), caller_pcm(state, text, fmt.sample_rate), silence(fmt, tail_ms)]
    )
    return encode(pcm, fmt)


def chunked(audio: bytes, fmt: SttFormat, ms: int = 100) -> Iterator[bytes]:
    """``audio`` cut into ``ms`` pieces (the last one shorter)."""
    width = fmt.sample_rate * ms // 1000 * (1 if fmt.encoding == "ulaw" else 2)
    for start in range(0, len(audio), width):
        yield audio[start : start + width]


async def send_audio(
    ws: ClientConnection, audio: bytes, *, commit: bool = False, previous_text: str | None = None
) -> None:
    message: dict[str, object] = {
        "message_type": "input_audio_chunk",
        "audio_base_64": base64.b64encode(audio).decode("ascii"),
        "commit": commit,
    }
    if previous_text is not None:
        message["previous_text"] = previous_text
    await send(ws, message)


async def recv_type(ws: ClientConnection, message_type: str) -> tuple[Frame, list[Frame]]:
    """Read until a frame of ``message_type``; returns it and the frames skipped on the way."""
    skipped: list[Frame] = []
    while True:
        frame = await recv_json(ws)
        if frame["message_type"] == message_type:
            return frame, skipped
        skipped.append(frame)
