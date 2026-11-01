"""ElevenLabs Scribe realtime STT WebSocket client (specs/08 §1-§4, §7, §9; Decision P03-T11).

One :class:`ElevenLabsRealtimeStt` per call leg. While it is open a reader task runs per socket (frames, close
classification, reconnection) and one timer task runs deferred commits, the timestamp fallback, and the
closing of a rotated-out socket. Every socket write happens under one lock. Every audio byte sent is kept in a
replay buffer until the transcript of a later commit arrives, so a reconnect resends what the lost session may
not have transcribed (and re-sends an outstanding commit at its position).
"""

from __future__ import annotations

# jscpd:ignore-start  (the socket client imports overlap tts_ws)
import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Self

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, TracerProvider
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, InvalidHandshake, InvalidStatus

from va_common.logging import get_logger
from va_common.time import Clock, SystemClock

# jscpd:ignore-end  (the socket client imports overlap tts_ws)
from va_elevenlabs.stt_buffer import AudioBatcher, ReplayBuffer, pieces
from va_elevenlabs.stt_commits import CommitTracker, PendingCommit
from va_elevenlabs.stt_errors import (
    ACTIONS,
    MINIMAL,
    SttAuthError,
    SttError,
    SttProtocolError,
    SttUnavailableError,
    classify_close,
    fatal_error,
)
from va_elevenlabs.stt_metrics import NullSttMetrics, SttMetrics
from va_elevenlabs.stt_models import SttConfig, SttConnection, SttEvent, SttEventKind, SttWord
from va_elevenlabs.stt_wire import (
    Committed,
    CommittedWords,
    Entities,
    Ignored,
    Partial,
    ServerError,
    SessionStarted,
    audio_chunk,
    bytes_per_ms,
    parse_message,
    realtime_url,
    silence_frame,
)
from va_elevenlabs.ws_tasks import crash_of, run_timer, stop_tasks

__all__ = ["GROWN_BATCH_MS", "SHRUNK_BATCH_MS", "ElevenLabsRealtimeStt"]

GROWN_BATCH_MS: Final = 200
SHRUNK_BATCH_MS: Final = 50
_API_KEY_HEADER: Final = "xi-api-key"
_AUTH_STATUSES: Final = frozenset({401, 403})
_MAX_FRAME_BYTES: Final = 2**22
_MS: Final = 1000.0
_RECENT_COMMITS: Final = 8
_RETRYABLE: Final = (OSError, TimeoutError, InvalidHandshake)

_log = get_logger(__name__)

type _Outcome = SttError | tuple[float, ...]


class _AttemptFailedError(Exception):
    """A connection attempt failed in a way the next attempt may fix."""


@dataclass(eq=False)
class _Session:
    """One server session: its socket, when it started, and the ``previous_text`` its first chunk carries."""

    ws: ClientConnection
    session_id: str
    started_at: float
    previous_text: str | None
    healthy: bool = False


class ElevenLabsRealtimeStt:
    """:class:`va_elevenlabs.stt_models.StreamingStt` over the ElevenLabs realtime STT WebSocket."""

    def __init__(
        self,
        conn: SttConnection,
        *,
        clock: Clock | None = None,
        metrics: SttMetrics | None = None,
        tracer_provider: TracerProvider | None = None,
    ) -> None:
        self._conn = conn
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._metrics: SttMetrics = metrics if metrics is not None else NullSttMetrics()
        self._tracer = trace.get_tracer("va_elevenlabs", tracer_provider=tracer_provider)
        self._cfg: SttConfig | None = None
        self._batcher = AudioBatcher("ulaw_8000", conn.batch_ms)
        self._replay = ReplayBuffer("ulaw_8000", conn.replay_max_s)
        self._commits = CommitTracker(conn.min_commit_interval_s, conn.timestamp_grace_s)
        self._session: _Session | None = None
        self._draining: list[tuple[float, _Session]] = []
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._out: asyncio.Queue[SttEvent | None] = asyncio.Queue()
        self._recent: deque[SttEvent] = deque(maxlen=_RECENT_COMMITS)
        self._tasks: list[asyncio.Task[None]] = []
        self._span: Span | None = None
        self._terminal: SttError | None = None
        self._closed = False
        self._minimal = False
        self._rotating = False
        self._from_reconnect = False
        self._failures = 0
        self._reconnects = 0
        self._audio_bytes = 0
        self._context = ""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.close()

    # Properties.

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def session_id(self) -> str | None:
        """The current server session (``None`` before :meth:`open` and while reconnecting)."""
        return self._session.session_id if self._session is not None else None

    @property
    def reconnects(self) -> int:
        """Reconnect attempts made so far."""
        return self._reconnects

    @property
    def audio_seconds(self) -> float:
        """Caller audio accepted by :meth:`send_audio` (metering, specs/08 §9)."""
        if self._cfg is None:
            return 0.0
        return self._audio_bytes / bytes_per_ms(self._cfg.audio_format) / _MS

    @property
    def rotation_due(self) -> bool:
        """Whether the session is old enough for a proactive :meth:`rotate` (specs/08 §7)."""
        session = self._session
        if session is None:
            return False
        return self._clock.monotonic() - session.started_at >= self._conn.rotate_after_s

    # Public API (specs/08 §1).

    async def open(self, cfg: SttConfig) -> None:
        """Connect (retrying connect errors with the reconnect delays) and wait for ``session_started``."""
        if self._closed or self._cfg is not None:
            msg = "the STT client is closed or already open"
            raise RuntimeError(msg)
        self._cfg = cfg
        self._batcher = AudioBatcher(cfg.audio_format, self._conn.batch_ms)
        self._replay = ReplayBuffer(cfg.audio_format, self._conn.replay_max_s)
        self._context = (cfg.previous_text or "")[-self._conn.context_chars :]
        self._span = self._tracer.start_span(
            "stt.session", kind=SpanKind.CLIENT, attributes={"elevenlabs.model_id": self._conn.model_id}
        )
        try:
            session = await self._dial_initial(cfg.previous_text)
        except SttError as exc:
            self._fail(exc)
            raise
        self._install(session)
        self._spawn(self._timer_loop(), "stt-timer")

    async def send_audio(self, audio: bytes, *, commit: bool = False) -> None:
        """Queue whole codec frames; a message goes out once ``batch_ms`` is buffered. ``commit`` commits
        after adding ``audio``."""
        cfg = self._usable()
        if cfg.audio_format == "pcm_16000" and len(audio) % 2:
            msg = "PCM16 audio must contain whole 16-bit samples"
            raise ValueError(msg)
        self._audio_bytes += len(audio)
        async with self._lock:
            self._batcher.add(audio)
            if not commit and self._batcher.ready:
                await self._ship(self._batcher.take())
        if commit:
            await self.commit()

    async def commit(self) -> None:
        """Commit the user's turn now, or at the end of the 250 ms throttle window (merged)."""
        self._usable()
        now = self._clock.monotonic()
        if self._commits.request(now) is None:
            await self._send_commit(now)
            return
        async with self._lock:
            if self._batcher.ready:
                await self._ship(self._batcher.take())
        self._wake.set()

    async def events(self) -> AsyncIterator[SttEvent]:
        """Events in arrival order; ends after :meth:`close` or after the ``error`` event of a fatal error."""
        while True:
            item = await self._out.get()
            if item is None:
                self._out.put_nowait(None)
                return
            yield item

    async def close(self) -> None:
        """Stop the tasks and close every socket (code 1000). Idempotent."""
        if self._closed:
            return
        self._closed = True
        await stop_tasks(self._tasks)
        sessions = [session for _, session in self._draining]
        if self._session is not None:
            sessions.append(self._session)
        self._draining.clear()
        for session in sessions:
            await session.ws.close()
        self._end_span(None)
        self._out.put_nowait(None)

    def add_context(self, text: str) -> None:
        """Remember conversation text (the agent's turns) for the ``previous_text`` of the next session."""
        stripped = text.strip()
        if stripped:
            self._context = f"{self._context} {stripped}".strip()[-self._conn.context_chars :]

    async def rotate(self) -> None:
        """Proactive rotation: open a second session, switch the sender to it, and close the old socket after
        ``rotate_drain_s``. A failure keeps the current session and yields a ``stt_warning``."""
        self._usable()
        current = self._session
        if current is None or self._rotating:
            return
        self._rotating = True
        try:
            fresh = await self._dial(0, self._context_text(), rotation=True)
        except (SttError, _AttemptFailedError) as exc:
            _log.warning("stt_rotation_failed", error=type(exc).__name__)
            self._emit_notice("warning", "stt_warning", "session rotation failed")
            return
        finally:
            self._rotating = False
        if self._closed or self._terminal is not None or self._session is not current:
            await fresh.ws.close()
            return
        async with self._lock:
            self._install(fresh)
        self._draining.append((self._clock.monotonic() + self._conn.rotate_drain_s, current))
        self._wake.set()

    # Guards.

    def _usable(self) -> SttConfig:
        if self._terminal is not None:
            raise self._terminal
        if self._closed:
            msg = "the STT client is closed"
            raise RuntimeError(msg)
        if self._cfg is None:
            msg = "open() the STT client first"
            raise RuntimeError(msg)
        return self._cfg

    def _running(self) -> bool:
        return not self._closed and self._terminal is None

    def _context_text(self) -> str | None:
        return self._context or None

    # Connecting.

    async def _dial_initial(self, previous_text: str | None) -> _Session:
        delays = (0.0, *self._conn.reconnect_delays_s)
        for attempt, delay in enumerate(delays):
            if delay > 0:
                await self._clock.sleep(delay)
            try:
                return await self._dial(attempt, previous_text, rotation=False)
            except _AttemptFailedError:
                continue
        msg = f"could not connect after {len(delays)} attempts"
        raise SttUnavailableError("unavailable", msg)

    async def _dial(self, attempt: int, previous_text: str | None, *, rotation: bool) -> _Session:
        """Connect and wait for ``session_started``; raises a fatal :class:`SttError` or
        :class:`_AttemptFailedError`."""
        cfg = self._usable()
        attributes: dict[str, str | int | bool] = {
            "elevenlabs.model_id": self._conn.model_id,
            "elevenlabs.attempt": attempt,
            "elevenlabs.rotation": rotation,
        }
        with self._tracer.start_as_current_span(
            "elevenlabs.stt.connect", kind=SpanKind.CLIENT, attributes=attributes
        ):
            ws = await self._connect(realtime_url(self._conn, cfg, minimal=self._minimal))
            try:
                started = await self._handshake(ws, cfg)
            except (SttError, _AttemptFailedError):
                await ws.close()
                raise
        return _Session(ws, started.session_id, self._clock.monotonic(), previous_text)

    async def _connect(self, url: str) -> ClientConnection:
        try:
            return await connect(
                url,
                additional_headers={_API_KEY_HEADER: self._conn.api_key},
                proxy=None,
                open_timeout=self._conn.open_timeout_s,
                close_timeout=self._conn.close_timeout_s,
                ping_interval=None,
                max_size=_MAX_FRAME_BYTES,
                compression=None,
            )
        except InvalidStatus as exc:
            status = exc.response.status_code
            if status in _AUTH_STATUSES:
                raise SttAuthError("auth_error", f"handshake rejected with HTTP {status}") from None
            _log.warning("stt_connect_failed", status=status)
            raise _AttemptFailedError from None
        except _RETRYABLE as exc:
            _log.warning("stt_connect_failed", error=type(exc).__name__)
            raise _AttemptFailedError from None

    async def _handshake(self, ws: ClientConnection, cfg: SttConfig) -> SessionStarted:
        try:
            async with asyncio.timeout(self._conn.open_timeout_s):
                raw = await ws.recv()
        except TimeoutError:
            raise _AttemptFailedError from None
        except ConnectionClosed:
            outcome = self._close_outcome(ws)
            if isinstance(outcome, SttError):
                raise outcome from None
            raise _AttemptFailedError from None
        message = parse_message(raw)
        if isinstance(message, ServerError):
            outcome_or_none = self._on_error(message)
            if isinstance(outcome_or_none, SttError):
                raise outcome_or_none
            raise _AttemptFailedError
        if not isinstance(message, SessionStarted):
            msg = f"expected session_started, got {type(message).__name__}"
            raise SttProtocolError("invalid_server_frame", msg)
        _verify(message, cfg)
        return message

    def _go_minimal(self, error: str) -> SttError | None:
        """Retry without keyterms and entity detection; a second invalid request is fatal."""
        if self._minimal:
            reason = error or "the request is invalid with a minimal config"
            return SttProtocolError("invalid_request", reason)
        _log.warning("stt_invalid_request", error=error)
        self._minimal = True
        return None

    def _install(self, session: _Session) -> None:
        self._session = session
        self._spawn(self._read_loop(session), f"stt-reader-{session.session_id}")

    def _spawn(self, work: Coroutine[Any, Any, None], name: str) -> None:
        task = asyncio.create_task(work, name=name)
        task.add_done_callback(self._task_done)
        self._tasks = [t for t in self._tasks if not t.done()]
        self._tasks.append(task)

    # Writing (lock held).

    async def _ship(self, payload: bytes) -> None:
        """Keep ``payload`` for replay and send it without a commit."""
        self._replay.append(payload)
        await self._write_chunk(payload, commit=False)

    async def _send_commit(self, requested_at: float) -> None:
        async with self._lock:
            cfg = self._usable()
            payload = self._batcher.take() or silence_frame(cfg.audio_format)
            mark = self._replay.append(payload)
            self._commits.sent(self._clock.monotonic(), mark, requested_at)
            await self._write_chunk(payload, commit=True)

    async def _write_chunk(self, payload: bytes, *, commit: bool) -> bool:
        """Send one chunk on the current session (``False`` when there is none or the write failed)."""
        session, cfg = self._session, self._cfg
        if session is None or cfg is None:
            return False
        message = audio_chunk(
            payload, commit=commit, sample_rate=cfg.sample_rate, previous_text=session.previous_text
        )
        try:
            async with asyncio.timeout(self._conn.send_timeout_s):
                await session.ws.send(message)
        except ConnectionClosed:
            return False
        except TimeoutError:
            _log.warning("stt_send_timeout")
            session.ws.transport.abort()
            return False
        session.previous_text = None
        return True

    # Reading.

    async def _read_loop(self, session: _Session) -> None:
        outcome = await self._pump(session)
        if self._closed or self._terminal is not None or session is not self._session:
            return
        if isinstance(outcome, SttError):
            self._fail(outcome)
            await session.ws.close()
            return
        await self._recover(session, outcome)

    async def _pump(self, session: _Session) -> _Outcome:
        """Handle frames until the socket closes or a frame ends the session; what happens next."""
        try:
            async for raw in session.ws:
                outcome = self._on_frame(session, raw)
                if outcome is not None:
                    return outcome
        except ConnectionClosedError:
            pass
        return self._close_outcome(session.ws)

    def _close_outcome(self, ws: ClientConnection) -> _Outcome:
        """A close with no preceding error frame: fatal, or reconnect (minimal config after ``invalid …``)."""
        _log.info("stt_socket_lost", close_code=ws.close_code, close_reason=ws.close_reason)
        verdict = classify_close(ws.close_code, ws.close_reason or "")
        if isinstance(verdict, SttError):
            self._metrics.vendor_error(verdict.code)
            return verdict
        if verdict == MINIMAL:
            self._metrics.vendor_error("invalid_request")
            error = self._go_minimal(ws.close_reason or "")
            if error is not None:
                return error
        return self._conn.reconnect_delays_s

    def _on_frame(self, session: _Session, raw: str | bytes) -> _Outcome | None:
        try:
            message = parse_message(raw)
        except SttProtocolError as exc:
            return exc
        if isinstance(message, ServerError):
            return self._on_error(message)
        if isinstance(message, Partial):
            session.healthy = True
            self._emit(SttEvent(kind="partial", text=message.text, received_at=self._clock.monotonic()))
        elif isinstance(message, Committed):
            session.healthy = True
            self._commits.plain(message.text, self._clock.monotonic())
            self._wake.set()
        elif isinstance(message, CommittedWords):
            session.healthy = True
            self._on_words(message)
        elif isinstance(message, Entities):
            self._on_entities(message)
        elif isinstance(message, Ignored):
            _log.info("stt_message_ignored", message_type=message.message_type)
        else:
            _log.warning("stt_unexpected_session_started")
        return None

    def _on_words(self, message: CommittedWords) -> None:
        resolved = self._commits.with_words()
        if resolved is None:
            _log.info("stt_late_timestamps_dropped")
            return
        self._emit_committed(resolved.commit, message.text, message.words, message.language_code)

    def _on_entities(self, message: Entities) -> None:
        for event in reversed(self._recent):
            if event.text == message.text:
                update = {
                    "entities": list(message.entities),
                    "is_update": True,
                    "received_at": self._clock.monotonic(),
                }
                self._emit(event.model_copy(update=update))
                return
        _log.info("stt_entities_unmatched")

    def _on_error(self, message: ServerError) -> _Outcome | None:
        self._metrics.vendor_error(message.message_type)
        action = ACTIONS[message.message_type]
        level = "error" if action in {"fatal", "log"} else "warning"
        getattr(_log, level)("stt_server_error", message_type=message.message_type, error=message.error)
        return self._handlers()[action](message)

    def _handlers(self) -> dict[str, Callable[[ServerError], _Outcome | None]]:
        return {
            "fatal": lambda m: fatal_error(m.message_type, m.error),
            "reconnect": lambda _: self._conn.reconnect_delays_s,
            "backoff": lambda _: self._conn.backoff_delays_s,
            "minimal": lambda m: self._go_minimal(m.error) or self._conn.reconnect_delays_s,
            "warn": self._on_warning,
            "shrink": lambda _: self._batcher.resize(SHRUNK_BATCH_MS),
            "grow": lambda _: self._batcher.resize(GROWN_BATCH_MS),
            "throttled": self._on_throttled,
            "log": lambda _: None,
        }

    def _on_warning(self, message: ServerError) -> None:
        self._emit_notice("warning", "stt_warning", message.error)

    def _on_throttled(self, message: ServerError) -> None:
        del message
        self._commits.throttled(self._clock.monotonic())
        self._wake.set()

    # Reconnecting.

    async def _recover(self, lost: _Session, delays: Sequence[float]) -> None:
        """Reconnect after ``lost`` ended; audio produced meanwhile is kept for the replay."""
        self._session = None
        self._emit_notice("warning", "stt_reconnect", "reconnecting the STT session")
        await lost.ws.close()
        age = self._clock.monotonic() - lost.started_at
        if lost.healthy or age >= self._conn.healthy_after_s:
            self._failures = 0
        elif self._from_reconnect:
            self._failures += 1
        while self._failures < len(delays):
            await self._clock.sleep(delays[self._failures])
            if self._closed:
                return
            self._reconnects += 1
            self._metrics.reconnect()
            try:
                fresh = await self._dial(self._reconnects, self._context_text(), rotation=False)
            except SttError as exc:
                self._fail(exc)
                return
            except _AttemptFailedError:
                self._failures += 1
                continue
            self._from_reconnect = True
            await self._resume(fresh)
            return
        self._fail(SttUnavailableError("unavailable", f"{len(delays)} reconnect attempts failed"))

    async def _resume(self, session: _Session) -> None:
        """Switch to ``session`` and replay the kept audio, re-sending an outstanding commit at its mark."""
        async with self._lock:
            if self._closed or self._terminal is not None:
                await session.ws.close()
                return
            for awaiting in self._commits.flush_awaiting():
                self._emit_committed(awaiting.commit, awaiting.text, (), None)
            self._commits.collapse(self._clock.monotonic())
            self._install(session)
            before, after = self._replay.split(self._commits.last_mark)
            size = self._batcher.batch_bytes
            if self._commits.outstanding:
                parts = list(pieces(before, size)) or [silence_frame(self._usable().audio_format)]
                for part in parts[:-1]:
                    await self._write_chunk(part, commit=False)
                await self._write_chunk(parts[-1], commit=True)
            for part in pieces(after, size):
                await self._write_chunk(part, commit=False)

    # Timer.

    async def _timer_loop(self) -> None:
        await run_timer(self._clock, self._wake, self._run_due, self._running)

    async def _run_due(self) -> float | None:
        """Send a due deferred commit, emit expired plain transcripts, close drained sockets; seconds until
        the next of those deadlines (``None``: nothing pending)."""
        now = self._clock.monotonic()
        requested = self._commits.take_due(now)
        if requested is not None:
            await self._send_commit(requested)
        for awaiting in self._commits.expired(now):
            self._emit_committed(awaiting.commit, awaiting.text, (), None)
        for deadline, session in tuple(self._draining):
            if deadline <= now:
                self._draining.remove((deadline, session))
                await session.ws.close()
        deadlines = [deadline for deadline, _ in self._draining]
        commit_deadline = self._commits.next_deadline()
        if commit_deadline is not None:
            deadlines.append(commit_deadline)
        return min(deadlines) - now if deadlines else None

    # Events.

    def _emit_committed(
        self, commit: PendingCommit | None, text: str, words: Sequence[SttWord], language: str | None
    ) -> None:
        now = self._clock.monotonic()
        if commit is not None:
            self._replay.ack(commit.mark)
            self._metrics.final_ms((now - commit.requested_at) * _MS)
        event = SttEvent(
            kind="committed", text=text, words=list(words), language_code=language, received_at=now
        )
        self._recent.append(event)
        self.add_context(text)
        self._emit(event)

    def _emit_notice(self, kind: SttEventKind, error_type: str, text: str) -> None:
        self._emit(SttEvent(kind=kind, text=text, error_type=error_type, received_at=self._clock.monotonic()))

    def _emit(self, event: SttEvent) -> None:
        if self._terminal is None and not self._closed:
            self._out.put_nowait(event)

    # Termination.

    def _fail(self, error: SttError) -> None:
        if self._terminal is not None or self._closed:
            return
        _log.warning("stt_session_failed", code=error.code, error_type=type(error).__name__)
        self._emit_notice("error", error.code, error.message)
        self._terminal = error
        self._out.put_nowait(None)
        self._end_span(error)
        self._wake.set()

    def _task_done(self, task: asyncio.Task[None]) -> None:
        exc = crash_of(task)
        if exc is not None:
            _log.error("stt_task_crashed", task=task.get_name(), exc_info=exc)
            self._fail(SttError("client_error", f"{task.get_name()} task crashed: {type(exc).__name__}"))

    def _end_span(self, error: SttError | None) -> None:
        span, self._span = self._span, None
        if span is None:
            return
        span.set_attribute("va.stt.reconnects", self._reconnects)
        if error is not None:
            span.set_status(Status(StatusCode.ERROR, error.code))
        span.end()


def _verify(started: SessionStarted, cfg: SttConfig) -> None:
    """The echoed config must match what we asked for (specs/08 §4 ``session_started``)."""
    expected = (cfg.audio_format, cfg.sample_rate, "manual")
    echoed = (started.audio_format, started.sample_rate, started.commit_strategy)
    if echoed != expected:
        msg = f"session_started echoed {echoed}, expected {expected}"
        raise SttProtocolError("config_mismatch", msg)
