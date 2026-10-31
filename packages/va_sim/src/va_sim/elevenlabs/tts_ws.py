"""FakeElevenLabs TTS multi-context WebSocket (specs/30 §3.3, specs/09 §2-§3, §8; Decisions P03-T04 3-10,
P03-T05c rule 9).

``GET /v1/text-to-speech/{voice_id}/multi-stream-input``. The socket is always accepted; failures close it
with code 1008 (an unknown voice after a ``voice_not_found`` error frame; 1011 for injected faults, 1012 on
``/sim/reset``). Each context has a worker task that generates its triggered texts in order; contexts
interleave freely and every frame goes out under one send lock. The first chunk of a context's first
generation waits ``tts_ttfa_ms`` on the sim clock after its trigger.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from starlette.websockets import WebSocket

from va_sim.elevenlabs.auth import API_KEY_HEADER, INVALID_API_KEY, key_from_message
from va_sim.elevenlabs.store import EndReason, FaultKind
from va_sim.elevenlabs.tts_audio import chunks
from va_sim.elevenlabs.tts_context import MAX_CONTEXTS, TtsContext, parse_schedule
from va_sim.elevenlabs.tts_params import TtsParams, parse_params
from va_sim.elevenlabs.voice_json import voice_not_found_message
from va_sim.elevenlabs.ws_io import (
    INTERNAL_ERROR,
    NORMAL_CLOSURE,
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

__all__ = ["INACTIVITY_MESSAGE", "VOICE_NOT_FOUND", "TtsSocket", "tts_websocket"]

INACTIVITY_MESSAGE: Final = "Have not received a new text input within the timeout of {seconds} seconds."
DEFAULT_CONTEXT: Final = "default"
VOICE_NOT_FOUND: Final = "voice_not_found"
_INVALID_MESSAGE: Final = "invalid message"
_WS_FAULTS: Final[frozenset[FaultKind]] = frozenset({"disconnect", "error", "delay"})
_MS_PER_S: Final = 1000


@dataclass(frozen=True, slots=True)
class _Generation:
    text: str
    triggered_at: float


@dataclass(slots=True, eq=False)
class _Live:
    """A context's runtime: its generation queue and progress (hashed by identity)."""

    ctx: TtsContext
    queue: asyncio.Queue[_Generation | None] = field(default_factory=asyncio.Queue)
    stopped: bool = False
    busy: bool = False
    first_done: bool = False
    interrupted: bool = False
    chunks_sent: int = 0
    end_reason: EndReason = "close_context"

    def pending(self) -> bool:
        """Whether ending now drops text or audio."""
        return self.interrupted or self.busy or not self.queue.empty() or bool(self.ctx.buffer.strip())


@dataclass(frozen=True, slots=True)
class _Close:
    code: int | None
    reason: str
    end_reason: EndReason


def _error_frame(message: str, error: str, code: int) -> Message:
    return {"message": message, "error": error, "code": code}


class TtsSocket:
    """One authenticated multi-context socket."""

    def __init__(self, ws: WebSocket, state: SimState, voice_id: str, params: TtsParams) -> None:
        self._ws = ws
        self._state = state
        self._clock = state.clock
        self._voice_id = voice_id
        self._params = params
        self._open: dict[str, _Live] = {}
        self._closing: set[_Live] = set()
        self._closed_ids: set[str] = set()
        self._workers: set[asyncio.Task[None]] = set()
        self._send_lock = asyncio.Lock()
        self._done = asyncio.Event()
        self._close: _Close | None = None
        self._last_text = self._clock.monotonic()
        self._group: asyncio.TaskGroup | None = None

    def close_for_reset(self) -> None:
        self._finish(SERVICE_RESTART, "simulator reset", "reset")

    def _finish(self, code: int | None, reason: str, end_reason: EndReason) -> None:
        if self._close is None:
            self._close = _Close(code, reason, end_reason)
            self._done.set()

    async def run(self, first: Message | None) -> None:
        """Serve the socket until it closes; ``first`` is the message already read for authentication."""
        store = self._state.elevenlabs
        store.sockets.add(self)
        try:
            async with asyncio.TaskGroup() as group:
                self._group = group
                tasks = (group.create_task(self._read(first)), group.create_task(self._watchdog()))
                await self._done.wait()
                for task in (*tasks, *self._workers):
                    task.cancel()
        finally:
            store.sockets.discard(self)
            self._end_open()
        if self._close is not None and self._close.code is not None:
            await close_quietly(self._ws, self._close.code, self._close.reason)

    def _end_open(self) -> None:
        reason: EndReason = self._close.end_reason if self._close is not None else "disconnect"
        now = self._clock.virtual_ms()
        for live in (*self._open.values(), *self._closing):
            live.ctx.record.end(reason, now, interrupted=live.pending())

    async def _send(self, payload: Message) -> None:
        if self._close is not None:
            return
        async with self._send_lock:
            if not await send_json(self._ws, payload):
                self._finish(None, "", "disconnect")

    async def _error(self, message: str, error: str) -> None:
        await self._send(_error_frame(message, error, POLICY_VIOLATION))

    # Reading.

    async def _read(self, first: Message | None) -> None:
        message = first
        while True:
            if message is None:
                received = await receive_json(self._ws)
                if received == "disconnect":
                    self._finish(None, "", "disconnect")
                    return
                if received == "invalid":
                    self._finish(POLICY_VIOLATION, _INVALID_MESSAGE, "disconnect")
                    return
                message = received
            if "text" in message:
                self._last_text = self._clock.monotonic()
            if message.get("close_socket") is True:
                await self._close_socket()
                return
            await self._handle(message)
            message = None

    async def _handle(self, message: Message) -> None:
        live = await self._resolve(message)
        if live is None:
            return
        text = message.get("text")
        if isinstance(text, str) and text:
            live.ctx.add_text(text)
        if message.get("close_context") is True:
            self._close_context(live)
            return
        generated = live.ctx.take(flush=message.get("flush") is True)
        if generated is not None:
            live.queue.put_nowait(_Generation(generated, self._clock.monotonic()))

    async def _resolve(self, message: Message) -> _Live | None:
        context_id = message.get("context_id", DEFAULT_CONTEXT)
        if not isinstance(context_id, str) or not context_id:
            await self._error("context_id must be a non-empty string", "invalid_message")
            return None
        text = message.get("text")
        if text is not None and not isinstance(text, str):
            await self._error("text must be a string", "invalid_message")
            return None
        if context_id in self._closed_ids:
            await self._error(f"Context {context_id} has already been closed", "context_closed")
            return None
        live = self._open.get(context_id)
        return live if live is not None else await self._create(context_id, message)

    async def _create(self, context_id: str, message: Mapping[str, object]) -> _Live | None:
        if len(self._open) >= MAX_CONTEXTS:
            await self._error(
                f"Maximum of {MAX_CONTEXTS} concurrent contexts per connection exceeded",
                "max_active_conversations",
            )
            return None
        try:
            schedule = parse_schedule(message.get("generation_config"))
        except ValueError as exc:
            await self._error(str(exc), "invalid_message")
            return None
        record = self._state.elevenlabs.new_request(
            transport="ws",
            voice_id=self._voice_id,
            model_id=self._params.model_id,
            output_format=self._params.output_format.name,
            context_id=context_id,
            started_ms=self._clock.virtual_ms(),
        )
        live = _Live(TtsContext(context_id, schedule, record))
        if self._group is None:
            msg = "contexts are created only while the socket runs"
            raise RuntimeError(msg)
        worker = self._group.create_task(self._work(live), name=f"tts-context-{context_id}")
        self._workers.add(worker)
        worker.add_done_callback(self._workers.discard)
        self._open[context_id] = live
        return live

    def _close_context(self, live: _Live) -> None:
        del self._open[live.ctx.id]
        self._closed_ids.add(live.ctx.id)
        dropped = live.ctx.discard()
        queued = False
        while not live.queue.empty():
            live.queue.get_nowait()
            queued = True
        live.interrupted = live.interrupted or dropped or queued
        live.stopped = True
        live.end_reason = "close_context"
        self._closing.add(live)
        live.queue.put_nowait(None)

    async def _close_socket(self) -> None:
        for live in tuple(self._open.values()):
            live.interrupted = live.ctx.discard() or live.interrupted
            live.end_reason = "close_socket"
            self._closing.add(live)
            live.queue.put_nowait(None)
        self._open.clear()
        if self._workers:
            await asyncio.wait(tuple(self._workers))
        self._finish(NORMAL_CLOSURE, "", "close_socket")

    async def _watchdog(self) -> None:
        timeout = self._params.inactivity_timeout
        while True:
            remaining = self._last_text + timeout - self._clock.monotonic()
            if remaining <= 0:
                self._finish(POLICY_VIOLATION, INACTIVITY_MESSAGE.format(seconds=timeout), "inactivity")
                return
            await self._clock.sleep(remaining)

    # Generating.

    async def _work(self, live: _Live) -> None:
        while True:
            item = await live.queue.get()
            if item is None:
                await self._send({"isFinal": True, "contextId": live.ctx.id})
                live.ctx.record.end(live.end_reason, self._clock.virtual_ms(), interrupted=live.interrupted)
                self._closing.discard(live)
                return
            live.busy = True
            try:
                await self._generate(live, item)
            finally:
                live.busy = False

    async def _generate(self, live: _Live, item: _Generation) -> None:
        async with hold_clock(self._clock, self._state.tasks, name=f"tts-synth-{live.ctx.id}"):
            pieces = await asyncio.to_thread(chunks, item.text, self._params.output_format)
        if not live.first_done:
            live.first_done = True
            ttfa_s = self._state.latencies.tts_ttfa_ms / _MS_PER_S
            remaining = item.triggered_at + ttfa_s - self._clock.monotonic()
            if remaining > 0:
                await self._clock.sleep(remaining)
        for piece in pieces:
            # Re-check after the fault: a delay fault can outlast a close_context.
            if live.stopped or not await self._fault_allows(live) or live.stopped:
                live.interrupted = True
                return
            alignment = piece.alignment
            await self._send(
                {
                    "audio": base64.b64encode(piece.audio).decode("ascii"),
                    "contextId": live.ctx.id,
                    "alignment": alignment,
                    "normalizedAlignment": alignment,
                }
            )
            live.chunks_sent += 1

    async def _fault_allows(self, live: _Live) -> bool:
        """Fire an armed fault before the next chunk; ``False`` when the socket is being torn down."""
        fault = self._state.elevenlabs.take_fault(_WS_FAULTS, live.chunks_sent)
        if fault is None:
            return True
        if fault.kind == "delay":
            await self._clock.sleep(fault.delay_ms / _MS_PER_S)
            return True
        if fault.kind == "error":
            await self._send(_error_frame("Simulated TTS failure", "internal_error", INTERNAL_ERROR))
            self._finish(INTERNAL_ERROR, "Simulated TTS failure", "fault")
        else:
            self._finish(INTERNAL_ERROR, "simulated disconnect", "fault")
        return False


async def tts_websocket(state: SimState, ws: WebSocket) -> None:
    """Accept, validate the query, the API key, and the voice, then serve the socket."""
    await ws.accept()
    try:
        params = parse_params(ws.query_params)
    except ValueError as exc:
        await close_quietly(ws, POLICY_VIOLATION, str(exc))
        return
    first: Message | None = None
    key = ws.headers.get(API_KEY_HEADER)
    if key is None:
        received = await receive_json(ws)
        if received == "disconnect":
            return
        if received == "invalid":
            await close_quietly(ws, POLICY_VIOLATION, _INVALID_MESSAGE)
            return
        first = received
        key = key_from_message(first)
    if not state.elevenlabs.accepts(key):
        await close_quietly(ws, POLICY_VIOLATION, INVALID_API_KEY)
        return
    voice_id: str = ws.path_params["voice_id"]
    if state.elevenlabs.voices.get(voice_id) is None:
        frame = _error_frame(voice_not_found_message(voice_id), VOICE_NOT_FOUND, POLICY_VIOLATION)
        if await send_json(ws, frame):
            await close_quietly(ws, POLICY_VIOLATION, VOICE_NOT_FOUND)
        return
    await TtsSocket(ws, state, voice_id, params).run(first)
