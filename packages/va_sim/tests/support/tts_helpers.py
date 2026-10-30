"""FakeElevenLabs TTS test helpers: a served simulator, a WebSocket client, frame readers, audio decoding.

WebSocket tests run the simulator on real sockets (``running_sims`` on an ephemeral port). Readers wait with a
wall-clock timeout so a broken simulator fails instead of hanging.
"""

from __future__ import annotations

import asyncio
import base64
import math
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlencode

import httpx
import numpy as np
import orjson
from numpy.typing import NDArray
from starlette.applications import Starlette
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from va_sim.audio.codec import ulaw_decode
from va_sim.clock import RealtimeClock
from va_sim.elevenlabs.tts_audio import OutputFormat
from va_sim.server import running_sims, simulator_routes
from va_sim.settings import Latencies
from va_sim.state import SimState

__all__ = [
    "API_KEY",
    "RECV_TIMEOUT_S",
    "VOICE_ID",
    "Closed",
    "audio_frames",
    "decode_pcm",
    "joined_audio",
    "realtime_state",
    "recv_json",
    "recv_spoken",
    "recv_until_final",
    "rest_client",
    "send",
    "served",
    "socket_url",
    "text_of",
    "tts_socket",
    "wait_closed",
]

API_KEY: Final = "sim-elevenlabs-key"
VOICE_ID: Final = "voice-1"
RECV_TIMEOUT_S: Final = 10.0

type Frame = dict[str, Any]


def realtime_state(tts_ttfa_ms: int = 0) -> SimState:
    """A state on the wall clock (WebSocket protocol tests) with the given first-audio latency."""
    latencies = Latencies(stt_final_ms=10, tts_ttfa_ms=tts_ttfa_ms, ttft_ms=20, tokens_per_s=1000.0)
    return SimState(clock=RealtimeClock(), latencies=latencies)


@asynccontextmanager
async def served(state: SimState) -> AsyncIterator[str]:
    """FakeElevenLabs on an ephemeral port; yields its ``ws://`` base URL."""
    async with running_sims(state, ports={"elevenlabs": 0}) as urls:
        yield urls["elevenlabs"].replace("http://", "ws://", 1)


def socket_url(base: str, query: Mapping[str, str] | None = None, voice_id: str = VOICE_ID) -> str:
    suffix = f"?{urlencode(dict(query))}" if query else ""
    return f"{base}/v1/text-to-speech/{voice_id}/multi-stream-input{suffix}"


@asynccontextmanager
async def tts_socket(
    base: str, query: Mapping[str, str] | None = None, *, key: str | None = API_KEY, voice_id: str = VOICE_ID
) -> AsyncIterator[ClientConnection]:
    """A client socket (``xi-api-key`` header unless ``key`` is None)."""
    headers = {"xi-api-key": key} if key is not None else {}
    async with connect(
        socket_url(base, query, voice_id), additional_headers=headers, max_size=2**24, ping_interval=None
    ) as ws:
        yield ws


async def send(ws: ClientConnection, message: Mapping[str, object]) -> None:
    await ws.send(orjson.dumps(dict(message)).decode())


async def recv_json(ws: ClientConnection) -> Frame:
    raw = await _recv(ws)
    frame: Frame = orjson.loads(raw)
    return frame


async def _recv(ws: ClientConnection) -> str | bytes:
    async with asyncio.timeout(RECV_TIMEOUT_S):
        return await ws.recv()


async def recv_until_final(ws: ClientConnection, context_id: str) -> list[Frame]:
    """Every frame up to and including ``context_id``'s ``isFinal``."""
    frames: list[Frame] = []
    while True:
        frame = await recv_json(ws)
        frames.append(frame)
        if frame.get("isFinal") and frame.get("contextId") == context_id:
            return frames


async def recv_spoken(ws: ClientConnection, context_id: str, text: str) -> list[Frame]:
    """Every frame until all of ``text``'s audio for ``context_id`` arrived, then close the context and read
    up to its ``isFinal``."""
    expected = math.ceil(len(text) * 60 / 200)
    frames: list[Frame] = []
    while len(audio_frames(frames, context_id)) < expected:
        frames.append(await recv_json(ws))
    await send(ws, {"context_id": context_id, "close_context": True})
    return [*frames, *await recv_until_final(ws, context_id)]


@dataclass(frozen=True, slots=True)
class Closed:
    code: int | None
    reason: str | None
    frames: tuple[Frame, ...]


async def wait_closed(ws: ClientConnection) -> Closed:
    """Read until the server closes; the close code/reason and the frames received meanwhile."""
    frames: list[Frame] = []
    try:
        while True:
            frames.append(orjson.loads(await _recv(ws)))
    except ConnectionClosed:
        pass
    return Closed(ws.close_code, ws.close_reason, tuple(frames))


def audio_frames(frames: list[Frame] | tuple[Frame, ...], context_id: str | None = None) -> list[Frame]:
    return [
        frame
        for frame in frames
        if "audio" in frame and (context_id is None or frame["contextId"] == context_id)
    ]


def text_of(frames: list[Frame]) -> str:
    """The text the frames' alignments spell, in order."""
    return "".join("".join(frame["alignment"]["chars"]) for frame in frames if frame["alignment"] is not None)


def decode_pcm(fmt: OutputFormat, audio: bytes) -> NDArray[np.int16]:
    """int16 samples of ``ulaw_8000`` or ``pcm_*`` audio."""
    if fmt.codec == "ulaw":
        return ulaw_decode(audio)
    return np.frombuffer(audio, dtype="<i2").astype(np.int16)


def joined_audio(frames: list[Frame]) -> bytes:
    return b"".join(base64.b64decode(frame["audio"]) for frame in frames)


@asynccontextmanager
async def rest_client(state: SimState, key: str | None = API_KEY) -> AsyncIterator[httpx.AsyncClient]:
    """An in-process client of FakeElevenLabs (``xi-api-key`` header unless ``key`` is None); closes the state
    afterwards."""
    app = Starlette(routes=simulator_routes("elevenlabs", state))
    headers = {"xi-api-key": key} if key is not None else {}
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://elevenlabs", headers=headers
        ) as client:
            yield client
    finally:
        await state.aclose()
