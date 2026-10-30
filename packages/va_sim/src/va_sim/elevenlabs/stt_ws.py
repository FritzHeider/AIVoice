"""FakeElevenLabs realtime STT WebSocket (specs/30 §3.3, specs/08 §2-§4, §10; Decision P03-T05a rules 1-11).

``GET /v1/speech-to-text/realtime``. The socket is always accepted; an invalid query or API key gets an
``invalid_request`` / ``auth_error`` frame, then close 1008. An authenticated session sends
``session_started``, then a reader task validates ``input_audio_chunk`` messages and feeds their audio to the
session's :class:`SttStream` (in a worker thread under ``hold_clock``), sending partials as they arise and
queueing commits; a committer task sends each commit ``stt_final_ms`` (sim clock) after it was read, in order.
Every frame goes out under one send lock. There is no server inactivity timeout; errors come only from armed
faults.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import numpy as np
from numpy.typing import NDArray
from starlette.websockets import WebSocket

from va_sim.elevenlabs.auth import API_KEY_HEADER, INVALID_API_KEY, MISSING_KEY
from va_sim.elevenlabs.stt_params import SttFormat, SttParams, config_json, parse_stt_params
from va_sim.elevenlabs.stt_store import (
    FATAL_1008,
    FATAL_1011,
    SttEndReason,
    SttFault,
    SttSessionRecord,
    SttTranscript,
)
from va_sim.elevenlabs.stt_stream import (
    UNINTELLIGIBLE,
    Commit,
    Heard,
    Partial,
    SttStream,
    decode_base64,
    decode_chunk,
)
from va_sim.elevenlabs.stt_text import find_entities, words_json
from va_sim.elevenlabs.ws_io import (
    INTERNAL_ERROR,
    POLICY_VIOLATION,
    SERVICE_RESTART,
    Message,
    close_quietly,
    receive_json,
    send_json,
)
from va_sim.tasks import hold_clock

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["Chunk", "InputProblem", "SttSocket", "parse_chunk", "stt_websocket"]

MISSING_KEY_REASON: Final = "Missing API key"
CHUNK_TOO_LONG: Final = "Audio chunk exceeds 1000 ms"
PREVIOUS_TEXT_LATE: Final = "previous_text can only be sent with the first audio chunk"
_MAX_CHUNK_MS: Final = 1000
_MS_PER_S: Final = 1000
_DEFAULT_LANGUAGE: Final = "en"


@dataclass(frozen=True, slots=True)
class Chunk:
    """A valid ``input_audio_chunk``: decoded samples, the commit flag, and ``previous_text`` (first only)."""

    audio: NDArray[np.int16]
    commit: bool
    previous_text: str | None


@dataclass(frozen=True, slots=True)
class InputProblem:
    """Why a message was dropped: the frame type (``input_error`` or ``chunk_size_exceeded``) and its text."""

    message_type: str
    error: str


def _problem(error: str) -> InputProblem:
    return InputProblem("input_error", error)


def _options(message: Message, *, first: bool, fmt: SttFormat) -> tuple[bool, str | None] | InputProblem:
    commit = message.get("commit", False)
    if not isinstance(commit, bool):
        return _problem("commit must be a boolean")
    sample_rate = message.get("sample_rate")
    if sample_rate is not None and (type(sample_rate) is not int or sample_rate != fmt.sample_rate):
        return _problem(f"sample_rate {sample_rate} does not match audio_format {fmt.name}")
    previous_text = message.get("previous_text")
    if previous_text is not None and not isinstance(previous_text, str):
        return _problem("previous_text must be a string")
    if previous_text is not None and not first:
        return _problem(PREVIOUS_TEXT_LATE)
    return commit, previous_text


def _audio(message: Message, fmt: SttFormat) -> NDArray[np.int16] | InputProblem:
    encoded = message.get("audio_base_64")
    if not isinstance(encoded, str):
        return _problem("audio_base_64 must be a base64 string")
    try:
        audio = decode_chunk(decode_base64(encoded), fmt)
    except ValueError as exc:
        return _problem(str(exc))
    if audio.size * _MS_PER_S > _MAX_CHUNK_MS * fmt.sample_rate:
        return InputProblem("chunk_size_exceeded", CHUNK_TOO_LONG)
    return audio


def parse_chunk(message: Message, *, first: bool, fmt: SttFormat) -> Chunk | InputProblem:
    """Validate one client message; ``first`` when no valid chunk was accepted yet in the session."""
    message_type = message.get("message_type")
    if message_type != "input_audio_chunk":
        return _problem(f"unsupported message_type {message_type!r}; expected 'input_audio_chunk'")
    options = _options(message, first=first, fmt=fmt)
    if isinstance(options, InputProblem):
        return options
    audio = _audio(message, fmt)
    if isinstance(audio, InputProblem):
        return audio
    commit, previous_text = options
    return Chunk(audio, commit, previous_text)


@dataclass(frozen=True, slots=True)
class _Close:
    code: int | None
    reason: str
    end_reason: SttEndReason


@dataclass(frozen=True, slots=True)
class _Pending:
    commit: Commit
    read_at: float


class SttSocket:
    """One authenticated realtime STT session."""

    def __init__(self, ws: WebSocket, state: SimState, params: SttParams) -> None:
        self._ws = ws
        self._state = state
        self._clock = state.clock
        self._params = params
        self._store = state.elevenlabs.stt
        self._language = params.language_code or _DEFAULT_LANGUAGE
        vad_ms = (
            round(params.vad_silence_threshold_secs * _MS_PER_S) if params.commit_strategy == "vad" else None
        )
        self._stream = SttStream(
            params.audio_format.sample_rate, self._resolve, language=self._language, vad_silence_ms=vad_ms
        )
        self.record = SttSessionRecord(
            id=state.ids.uuid().replace("-", ""),
            model_id=params.model_id,
            audio_format=params.audio_format.name,
            commit_strategy=params.commit_strategy,
            language_code=params.language_code,
            keyterms=list(params.keyterms),
            entity_detection=list(params.entity_detection),
            include_timestamps=params.include_timestamps,
            started_ms=self._clock.virtual_ms(),
        )
        self._commits: asyncio.Queue[_Pending] = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._done = asyncio.Event()
        self._close: _Close | None = None
        self._seen_chunk = False

    def close_for_reset(self) -> None:
        self._finish(SERVICE_RESTART, "simulator reset", "reset")

    def _finish(self, code: int | None, reason: str, end_reason: SttEndReason) -> None:
        if self._close is None:
            self._close = _Close(code, reason, end_reason)
            self._done.set()

    async def run(self) -> None:
        """Serve the session until it closes."""
        self._store.sessions.append(self.record)
        self._store.sockets.add(self)
        try:
            await self._send(
                {
                    "message_type": "session_started",
                    "session_id": self.record.id,
                    "config": config_json(self._params),
                }
            )
            async with asyncio.TaskGroup() as group:
                tasks = (group.create_task(self._read()), group.create_task(self._commit_worker()))
                await self._done.wait()
                for task in tasks:
                    task.cancel()
        finally:
            self._store.sockets.discard(self)
            self.record.end(self._close.end_reason if self._close else "disconnect", self._clock.virtual_ms())
        if self._close is not None and self._close.code is not None:
            await close_quietly(self._ws, self._close.code, self._close.reason)

    async def _send(self, payload: Message) -> None:
        """Send unless the session is closing; a failed send means the client left."""
        async with self._send_lock:
            sent = self._close is None and await send_json(self._ws, payload)
        if not sent:
            self._finish(None, "", "disconnect")

    # Reading.

    async def _read(self) -> None:
        while True:
            received = await receive_json(self._ws)
            if received == "disconnect":
                self._finish(None, "", "disconnect")
                return
            if received == "invalid":
                await self._send({"message_type": "input_error", "error": "message must be a JSON object"})
                continue
            chunk = parse_chunk(received, first=not self._seen_chunk, fmt=self._params.audio_format)
            if isinstance(chunk, InputProblem):
                await self._send({"message_type": chunk.message_type, "error": chunk.error})
                continue
            await self._accept(chunk)
            if self._close is not None:
                return

    async def _accept(self, chunk: Chunk) -> None:
        if not self._seen_chunk:
            self._seen_chunk = True
            self.record.previous_text = chunk.previous_text
        async with hold_clock(self._clock, self._state.tasks, name=f"stt-feed-{self.record.id}"):
            events = await asyncio.to_thread(self._stream.feed, chunk.audio)
        self.record.audio_ms = self._stream.audio_ms
        self.record.voiced_ms = self._stream.voiced_ms
        for event in events:
            if isinstance(event, Partial):
                await self._send({"message_type": "partial_transcript", "text": event.text})
                self.record.partials += 1
            else:
                self._queue(event)
        fault = self._store.take_fault(self._stream.audio_ms, on_commit=False)
        if fault is not None:
            await self._fire(fault)
        if chunk.commit and self._close is None:
            await self._commit()

    async def _commit(self) -> None:
        fault = self._store.take_fault(self._stream.audio_ms, on_commit=True)
        if fault is not None:
            await self._fire(fault)
            return
        async with hold_clock(self._clock, self._state.tasks, name=f"stt-commit-{self.record.id}"):
            commit = await asyncio.to_thread(self._stream.commit)
        self._queue(commit)

    def _queue(self, commit: Commit) -> None:
        self._commits.put_nowait(_Pending(commit, self._clock.monotonic()))

    async def _fire(self, fault: SttFault) -> None:
        if fault.kind == "disconnect":
            self._finish(INTERNAL_ERROR, "simulated disconnect", "fault")
            return
        error = f"Simulated {fault.kind}"
        await self._send({"message_type": fault.kind, "error": error})
        if fault.kind in FATAL_1008:
            self._finish(POLICY_VIOLATION, error, "fault")
        elif fault.kind in FATAL_1011:
            self._finish(INTERNAL_ERROR, error, "fault")

    def _resolve(self, marker: int | None) -> Heard:
        """What a segment with ``marker`` said (runs in the stream's worker thread; reads are GIL-atomic)."""
        if marker is not None:
            utterance = self._state.utterances.get(marker)
            if utterance is None:
                return Heard(UNINTELLIGIBLE, self._language, None, has_marker=True)
            return Heard(utterance.text, utterance.language or self._language, marker, has_marker=True)
        try:
            text = self._store.script.popleft()
        except IndexError:
            text = UNINTELLIGIBLE
        return Heard(text, self._language, None, has_marker=False)

    # Committing.

    async def _commit_worker(self) -> None:
        latency_s = self._state.latencies.stt_final_ms / _MS_PER_S
        while True:
            pending = await self._commits.get()
            remaining = pending.read_at + latency_s - self._clock.monotonic()
            if remaining > 0:
                await self._clock.sleep(remaining)
            await self._send_commit(pending.commit)

    async def _send_commit(self, commit: Commit) -> None:
        await self._send({"message_type": "committed_transcript", "text": commit.text})
        if self._params.include_timestamps:
            await self._send(
                {
                    "message_type": "committed_transcript_with_timestamps",
                    "text": commit.text,
                    "language_code": commit.language,
                    "words": words_json(commit.words),
                }
            )
        if self._params.entity_detection and commit.text:
            entities = find_entities(commit.text, self._params.entity_detection)
            await self._send(
                {"message_type": "committed_transcript_entities", "text": commit.text, "entities": entities}
            )
        if self._close is None:
            self.record.commits += 1
            self.record.transcripts.append(
                SttTranscript(
                    commit.text, list(commit.utterance_ids), commit.language, self._clock.virtual_ms()
                )
            )


async def _reject(ws: WebSocket, message_type: str, error: str, reason: str) -> None:
    if await send_json(ws, {"message_type": message_type, "error": error}):
        await close_quietly(ws, POLICY_VIOLATION, reason)


async def stt_websocket(state: SimState, ws: WebSocket) -> None:
    """Accept, validate the query and the ``xi-api-key`` header, then serve the session."""
    await ws.accept()
    try:
        params = parse_stt_params(ws.query_params)
    except ValueError as exc:
        await _reject(ws, "invalid_request", str(exc), str(exc))
        return
    key = ws.headers.get(API_KEY_HEADER)
    if not key:
        await _reject(ws, "auth_error", MISSING_KEY, MISSING_KEY_REASON)
        return
    if not state.elevenlabs.accepts(key):
        await _reject(ws, "auth_error", INVALID_API_KEY, INVALID_API_KEY)
        return
    await SttSocket(ws, state, params).run()
