"""ElevenLabs multi-context TTS WebSocket client (specs/09 §1-§3, §8, §11, §12; Decision P03-T10).

One :class:`TtsSocket` per call leg and voice. Two supervised tasks run while it is open: the reader (frames,
close classification, reconnection) and the timer (keepalive, end-of-utterance ``close_context``, the no-audio
watchdog). Every socket write happens under one lock; client state is updated before the write, so a write
that fails because the socket died is repaired by the reconnect replay (unconfirmed clauses are resent under
the context's ``-r{n}`` wire id).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Final, Self

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, TracerProvider
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, InvalidHandshake, InvalidStatus

from va_common.logging import get_logger
from va_common.time import Clock, SystemClock
from va_elevenlabs.tts_context import ContextState
from va_elevenlabs.tts_errors import (
    TtsAuthError,
    TtsError,
    TtsProtocolError,
    TtsUnavailableError,
    classify_close,
    classify_error,
    is_fatal,
)
from va_elevenlabs.tts_metrics import NullTtsMetrics, TtsMetrics
from va_elevenlabs.tts_models import OutputFormat, TtsAudio, TtsConfig, TtsVoice
from va_elevenlabs.tts_wire import (
    KEEPALIVE_CONTEXT,
    AudioFrame,
    ErrorFrame,
    FinalFrame,
    close_context_message,
    close_socket_message,
    flush_message,
    init_context,
    keepalive,
    keepalive_init,
    normalize_clause,
    parse_frame,
    socket_url,
    text_message,
)

__all__ = ["MAX_UTTERANCE_CONTEXTS", "TtsSocket"]

MAX_UTTERANCE_CONTEXTS: Final = 2
_API_KEY_HEADER: Final = "xi-api-key"
_AUTH_STATUSES: Final = frozenset({401, 403})
_MAX_FRAME_BYTES: Final = 2**24
_MS: Final = 1000.0
_RETRYABLE: Final = (OSError, TimeoutError, InvalidHandshake)

_log = get_logger(__name__)


class TtsSocket:
    """:class:`va_elevenlabs.tts_models.StreamingTts` over the ElevenLabs multi-context WebSocket."""

    def __init__(
        self,
        config: TtsConfig,
        *,
        clock: Clock | None = None,
        metrics: TtsMetrics | None = None,
        tracer_provider: TracerProvider | None = None,
    ) -> None:
        self._config = config
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._metrics: TtsMetrics = metrics if metrics is not None else NullTtsMetrics()
        self._tracer = trace.get_tracer("va_elevenlabs", tracer_provider=tracer_provider)
        self._voice: TtsVoice | None = None
        self._url = ""
        self._ws: ClientConnection | None = None
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._out: asyncio.Queue[TtsAudio | None] = asyncio.Queue()
        self._contexts: dict[str, ContextState] = {}
        self._by_wire: dict[str, ContextState] = {}
        self._spans: dict[str, Span] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._terminal: TtsError | None = None
        self._closed = False
        self._reconnects = 0
        self._failures = 0
        self._healthy = False
        self._from_reconnect = False
        self._last_text_at = 0.0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.close()

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` was called."""
        return self._closed

    @property
    def reconnects(self) -> int:
        """Reconnect attempts made so far."""
        return self._reconnects

    # Public API (specs/09 §1).

    async def open(self, output_format: OutputFormat, voice: TtsVoice) -> None:
        """Connect (retrying connect errors with the reconnect delays) and initialise the ``ka`` context."""
        if self._closed:
            msg = "the TTS socket is closed"
            raise RuntimeError(msg)
        if self._voice is not None:
            msg = "the TTS socket is already open"
            raise RuntimeError(msg)
        self._voice = voice
        self._url = socket_url(self._config, voice, output_format)
        ws = await self._dial_initial()
        await self._attach(ws)
        for name, work in (("reader", self._read_loop()), ("timer", self._timer_loop())):
            task = asyncio.create_task(work, name=f"tts-{name}")
            task.add_done_callback(self._task_done)
            self._tasks.append(task)

    async def start_context(self, context_id: str, voice: TtsVoice | None = None) -> None:
        """Initialise ``context_id`` (``voice`` may change only the voice settings); with two utterance
        contexts open the oldest is cancelled first."""
        socket_voice = self._usable()
        if voice is not None and _bound_part(voice) != _bound_part(socket_voice):
            msg = "a context cannot change the socket's voice, model, or language; open another socket"
            raise ValueError(msg)
        if context_id == KEEPALIVE_CONTEXT or context_id in self._contexts:
            msg = f"context {context_id!r} is reserved or already open"
            raise ValueError(msg)
        await self._make_room()
        settings = (voice or socket_voice).settings
        ctx = ContextState(context_id, settings, created_at=self._clock.monotonic())
        self._spans[context_id] = self._tracer.start_span(
            "elevenlabs.tts.context",
            kind=SpanKind.CLIENT,
            attributes={
                "elevenlabs.context_id": context_id,
                "elevenlabs.voice_id": socket_voice.provider_voice_id,
                "elevenlabs.model_id": socket_voice.model_id,
            },
        )
        async with self._lock:
            self._contexts[context_id] = ctx
            self._by_wire[ctx.wire_id] = ctx
            if await self._write(init_context(ctx.wire_id, settings, self._config.chunk_length_schedule)):
                self._last_text_at = self._clock.monotonic()
        self._wake.set()

    async def send(self, context_id: str, text: str, *, flush: bool = False) -> None:
        """Send one clause (normalized to end with exactly one space)."""
        self._usable()
        ctx = self._writable(context_id)
        clause = normalize_clause(text)
        async with self._lock:
            now = self._clock.monotonic()
            ctx.add(clause, flush=flush)
            if ctx.first_text_at is None:
                ctx.first_text_at = now
            if flush:
                ctx.progress_at = now
            await self._write_clause(ctx, clause, flush=flush)
        self._wake.set()

    async def flush(self, context_id: str) -> None:
        """End the utterance: generate everything sent; ``close_context`` follows once all audio arrived."""
        self._usable()
        ctx = self._writable(context_id)
        async with self._lock:
            now = self._clock.monotonic()
            ctx.ended = True
            ctx.ended_at = now
            if ctx.needs_bare_flush():
                ctx.mark_flushed()
                ctx.progress_at = now
                await self._write(flush_message(ctx.wire_id))
        self._wake.set()

    async def cancel(self, context_id: str) -> None:
        """Close ``context_id`` now and drop its late audio; its ``is_final`` still follows."""
        ctx = self._contexts.get(context_id)
        if ctx is None or ctx.cancelled or self._closed or self._terminal is not None:
            return
        ctx.cancelled = True
        async with self._lock:
            if not ctx.close_sent:
                ctx.close_sent = True
                await self._write(close_context_message(ctx.wire_id))
        self._wake.set()

    async def audio(self) -> AsyncIterator[TtsAudio]:
        """Audio and ``is_final`` markers in arrival order; ends after :meth:`close`, raises a fatal error."""
        while True:
            item = await self._out.get()
            if item is None:
                self._out.put_nowait(None)
                if self._terminal is not None:
                    raise self._terminal
                return
            yield item

    async def close(self) -> None:
        """Send ``close_socket``, wait briefly for the server to finish, then stop. Idempotent."""
        if self._closed:
            return
        self._closed = True
        ws = self._ws
        if ws is not None and self._terminal is None:
            async with self._lock:
                await self._write(close_socket_message())
            reader = self._tasks[0] if self._tasks else None
            if reader is not None:
                await asyncio.wait({reader}, timeout=self._config.close_timeout_s)
        await self._stop_tasks()
        if ws is not None:
            await ws.close()
        for context_id in tuple(self._spans):
            self._end_span(context_id, None)
        self._out.put_nowait(None)

    # Guards.

    def _usable(self) -> TtsVoice:
        if self._terminal is not None:
            raise self._terminal
        if self._closed:
            msg = "the TTS socket is closed"
            raise RuntimeError(msg)
        if self._voice is None:
            msg = "open() the TTS socket first"
            raise RuntimeError(msg)
        return self._voice

    def _writable(self, context_id: str) -> ContextState:
        ctx = self._contexts.get(context_id)
        if ctx is None or ctx.close_sent or ctx.ended:
            msg = f"context {context_id!r} is not open for text"
            raise ValueError(msg)
        return ctx

    async def _make_room(self) -> None:
        while True:
            live = [ctx for ctx in self._contexts.values() if not ctx.close_sent]
            if len(live) < MAX_UTTERANCE_CONTEXTS:
                return
            await self.cancel(live[0].logical_id)

    # Connecting.

    async def _dial_initial(self) -> ClientConnection:
        delays = (0.0, *self._config.reconnect_delays_s)
        for attempt, delay in enumerate(delays):
            if delay > 0:
                await self._clock.sleep(delay)
            try:
                return await self._connect(attempt)
            except _RETRYABLE as exc:
                _log.warning("tts_connect_failed", attempt=attempt, error=type(exc).__name__)
        msg = f"could not connect after {len(delays)} attempts"
        raise TtsUnavailableError("unavailable", msg)

    async def _connect(self, attempt: int) -> ClientConnection:
        voice = self._usable()
        attributes: dict[str, str | int] = {
            "elevenlabs.voice_id": voice.provider_voice_id,
            "elevenlabs.model_id": voice.model_id,
            "elevenlabs.attempt": attempt,
        }
        with self._tracer.start_as_current_span(
            "elevenlabs.tts.connect", kind=SpanKind.CLIENT, attributes=attributes
        ):
            try:
                return await connect(
                    self._url,
                    additional_headers={_API_KEY_HEADER: self._config.api_key},
                    proxy=None,
                    open_timeout=self._config.open_timeout_s,
                    close_timeout=self._config.close_timeout_s,
                    max_size=_MAX_FRAME_BYTES,
                )
            except InvalidStatus as exc:
                status = exc.response.status_code
                if status in _AUTH_STATUSES:
                    raise TtsAuthError("invalid_api_key", f"handshake rejected with HTTP {status}") from None
                raise

    async def _attach(self, ws: ClientConnection) -> None:
        """Make ``ws`` the current socket: ``ka`` first, then (after a reconnect) every open context."""
        async with self._lock:
            self._ws = ws
            self._healthy = False
            if await self._write(keepalive_init()):
                self._last_text_at = self._clock.monotonic()
            if self._from_reconnect:
                for ctx in tuple(self._contexts.values()):
                    await self._replay(ctx)
        self._wake.set()

    async def _replay(self, ctx: ContextState) -> None:
        """Re-initialise ``ctx`` on the new socket and resend its unconfirmed clauses (lock held)."""
        if ctx.close_sent or ctx.complete():
            self._finish(ctx)
            return
        self._by_wire.pop(ctx.wire_id, None)
        ctx.rewire(self._reconnects)
        self._by_wire[ctx.wire_id] = ctx
        if not await self._write(init_context(ctx.wire_id, ctx.settings, self._config.chunk_length_schedule)):
            return
        self._last_text_at = self._clock.monotonic()
        for index, clause in enumerate(ctx.clauses):
            if not clause.confirmed:
                flush = clause.flush or index + 1 == ctx.flushed_upto
                await self._write_clause(ctx, clause.text, flush=flush)
        if ctx.awaiting_audio():
            ctx.progress_at = self._clock.monotonic()

    async def _reconnect(self) -> ClientConnection | None:
        """Wait and reconnect; ``None`` after a fatal error, too many failed attempts, or :meth:`close`."""
        if self._healthy:
            self._failures = 0
        elif self._from_reconnect:
            self._failures += 1
        delays = self._config.reconnect_delays_s
        while self._failures < len(delays):
            await self._clock.sleep(delays[self._failures])
            if self._closed:
                return None
            self._reconnects += 1
            self._metrics.reconnect()
            try:
                ws = await self._connect(self._reconnects)
            except TtsError as exc:
                self._fail(exc)
                return None
            except _RETRYABLE as exc:
                _log.warning("tts_reconnect_failed", attempt=self._reconnects, error=type(exc).__name__)
                self._failures += 1
                continue
            self._from_reconnect = True
            return ws
        self._fail(TtsUnavailableError("unavailable", f"{len(delays)} reconnect attempts failed"))
        return None

    # Writing (lock held).

    async def _write(self, payload: str) -> bool:
        """Send ``payload`` on the current socket; ``False`` when there is none or it failed."""
        ws = self._ws
        if ws is None:
            return False
        try:
            async with asyncio.timeout(self._config.send_timeout_s):
                await ws.send(payload)
        except ConnectionClosed:
            return False
        except TimeoutError:
            _log.warning("tts_send_timeout")
            self._abort(ws)
            return False
        return True

    async def _write_clause(self, ctx: ContextState, clause: str, *, flush: bool) -> None:
        if await self._write(text_message(ctx.wire_id, clause, flush=flush)):
            self._last_text_at = self._clock.monotonic()
            ctx.characters += len(clause)
            self._metrics.characters(self._model(), len(clause))

    def _abort(self, ws: ClientConnection) -> None:
        """Drop ``ws`` without a close handshake; the reader then takes the reconnect path."""
        if ws is self._ws:
            ws.transport.abort()

    # Reading.

    async def _read_loop(self) -> None:
        while True:
            ws = self._ws
            if ws is None:
                return
            error = await self._pump(ws)
            self._ws = None
            if self._closed or self._terminal is not None:
                return
            if error is None:
                error = self._close_error(ws)
            if error is not None:
                self._fail(error)
                await ws.close()
                return
            _log.info("tts_socket_lost", close_code=ws.close_code, close_reason=ws.close_reason)
            if not await self._resume():
                return

    def _close_error(self, ws: ClientConnection) -> TtsError | None:
        """The fatal error ``ws``'s close stands for (counted), or ``None`` to reconnect."""
        error = classify_close(ws.close_code, ws.close_reason or "")
        if error is not None:
            self._metrics.vendor_error(error.code)
        return error

    async def _resume(self) -> bool:
        """Reconnect and replay; ``False`` when the socket stops instead."""
        fresh = await self._reconnect()
        if fresh is None:
            return False
        if self.closed:
            await fresh.close()
            return False
        await self._attach(fresh)
        return True

    async def _pump(self, ws: ClientConnection) -> TtsError | None:
        """Handle frames until ``ws`` closes (``None``) or a frame is fatal (the error)."""
        try:
            async for raw in ws:
                error = self._on_frame(raw)
                if error is not None:
                    return error
        except ConnectionClosedError:
            return None
        return None

    def _on_frame(self, raw: str | bytes) -> TtsError | None:
        try:
            frame = parse_frame(raw)
        except TtsProtocolError as exc:
            return exc
        if isinstance(frame, AudioFrame):
            self._on_audio(frame)
        elif isinstance(frame, FinalFrame):
            ctx = self._by_wire.get(frame.wire_id)
            if ctx is not None:
                self._finish(ctx)
        elif isinstance(frame, ErrorFrame):
            return self._on_error(frame)
        else:
            _log.info("tts_frame_ignored", keys=list(frame.keys))
        return None

    def _on_error(self, frame: ErrorFrame) -> TtsError | None:
        error = classify_error(frame.error, frame.message)
        self._metrics.vendor_error(frame.error)
        _log.warning("tts_server_error", error=frame.error, message=frame.message, code=frame.code)
        return error if is_fatal(error) else None

    def _on_audio(self, frame: AudioFrame) -> None:
        ctx = self._by_wire.get(frame.wire_id)
        if ctx is None:
            return
        if ctx.cancelled:
            self._metrics.late_dropped()
            return
        if not frame.audio:
            return
        now = self._clock.monotonic()
        self._healthy = True
        ctx.confirm(frame.alignment)
        if ctx.first_audio_at is None:
            ctx.first_audio_at = now
            if ctx.first_text_at is not None:
                self._metrics.ttfa(self._model(), (now - ctx.first_text_at) * _MS)
        ctx.last_audio_at = now
        ctx.progress_at = now
        self._emit(TtsAudio(ctx.logical_id, frame.audio, frame.alignment))
        self._wake.set()

    def _finish(self, ctx: ContextState) -> None:
        """The context is closed server-side (``isFinal``, or its socket is gone): emit ``is_final`` once."""
        if ctx.final_emitted:
            return
        ctx.final_emitted = True
        self._contexts.pop(ctx.logical_id, None)
        if self._by_wire.get(ctx.wire_id) is ctx:
            del self._by_wire[ctx.wire_id]
        self._end_span(ctx.logical_id, None, ctx)
        self._emit(TtsAudio(ctx.logical_id, b"", None, is_final=True))
        self._wake.set()

    # Timer.

    async def _timer_loop(self) -> None:
        while not self._closed and self._terminal is None:
            self._wake.clear()
            delay = await self._run_due()
            await self._nap(delay)

    async def _nap(self, delay: float | None) -> None:
        """Sleep on the injected clock for ``delay`` seconds (forever when ``None``) or until woken."""
        pending: set[asyncio.Task[object]] = {asyncio.create_task(self._wake.wait())}
        if delay is not None:
            pending.add(asyncio.create_task(self._clock.sleep(max(delay, 0.0))))
        try:
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def _run_due(self) -> float | None:
        """Run every due timer action; seconds until the next deadline (``None``: none pending)."""
        ws = self._ws
        if ws is None:
            return None
        now = self._clock.monotonic()
        deadlines: list[float] = []
        if not self._contexts:
            deadlines.append(await self._keepalive_due(now))
        for ctx in tuple(self._contexts.values()):
            if self._watchdog_fired(ctx, now, ws):
                return None
            tail = await self._tail_due(ctx, now)
            deadlines.extend(due for due in (_watchdog_deadline(ctx, self._config), tail) if due is not None)
        return min(deadlines) - now if deadlines else None

    async def _keepalive_due(self, now: float) -> float:
        due = self._last_text_at + self._config.keepalive_s
        if due > now:
            return due
        async with self._lock:
            if await self._write(keepalive()):
                self._last_text_at = now
                self._healthy = True
        return now + self._config.keepalive_s

    def _watchdog_fired(self, ctx: ContextState, now: float, ws: ClientConnection) -> bool:
        """Abort ``ws`` (reconnect path) when a flushed clause of ``ctx`` produced no audio in time."""
        due = _watchdog_deadline(ctx, self._config)
        if due is None or due > now:
            return False
        _log.warning("tts_no_audio", context_id=ctx.logical_id)
        self._metrics.vendor_error("no_audio_timeout")
        ctx.progress_at = now
        self._abort(ws)
        return True

    async def _tail_due(self, ctx: ContextState, now: float) -> float | None:
        """Send ``close_context`` once a complete utterance has been quiet long enough."""
        quiet = ctx.quiet_since()
        if ctx.close_sent or not ctx.complete() or quiet is None:
            return None
        window = self._config.tail_grace_s if ctx.all_aligned() else self._config.no_audio_timeout_s
        due = quiet + window
        if due > now:
            return due
        async with self._lock:
            if not ctx.close_sent:
                ctx.close_sent = True
                await self._write(close_context_message(ctx.wire_id))
        return None

    # Termination.

    def _emit(self, item: TtsAudio) -> None:
        if self._terminal is None and not self._closed:
            self._out.put_nowait(item)

    def _fail(self, error: TtsError) -> None:
        if self._terminal is not None or self._closed:
            return
        _log.warning("tts_socket_failed", code=error.code, error_type=type(error).__name__)
        self._terminal = error
        for context_id in tuple(self._spans):
            self._end_span(context_id, error)
        self._out.put_nowait(None)
        self._wake.set()

    def _task_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.error("tts_task_crashed", task=task.get_name(), exc_info=exc)
            self._fail(TtsError("client_error", f"{task.get_name()} task crashed: {type(exc).__name__}"))

    async def _stop_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _end_span(self, context_id: str, error: TtsError | None, ctx: ContextState | None = None) -> None:
        span = self._spans.pop(context_id, None)
        if span is None:
            return
        state = ctx if ctx is not None else self._contexts.get(context_id)
        if state is not None:
            span.set_attribute("elevenlabs.characters", state.characters)
            span.set_attribute("elevenlabs.reconnects", state.reconnects)
            if state.first_audio_at is not None and state.first_text_at is not None:
                span.set_attribute("elevenlabs.ttfa_ms", (state.first_audio_at - state.first_text_at) * _MS)
        if error is not None:
            span.set_status(Status(StatusCode.ERROR, error.code))
        span.end()

    def _model(self) -> str:
        return self._voice.model_id if self._voice is not None else ""


def _watchdog_deadline(ctx: ContextState, config: TtsConfig) -> float | None:
    """When ``ctx`` fails for lack of audio (``None``: it is not waiting for any)."""
    if ctx.cancelled or ctx.progress_at is None or not ctx.awaiting_audio():
        return None
    return ctx.progress_at + config.no_audio_timeout_s


def _bound_part(voice: TtsVoice) -> tuple[str, str, str | None]:
    """What the socket URL binds: voice, model, language."""
    return voice.provider_voice_id, voice.model_id, voice.language_code
