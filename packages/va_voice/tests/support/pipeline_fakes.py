"""In-memory fakes of the call pipeline's collaborators (transport, caller, VAD, STT, TTS, Brain, observer).

Everything runs on the simulators' :class:`va_sim.clock.AcceleratedClock`: virtual time advances only while
every task waits on the clock, so the fakes never sleep on the wall clock and results do not depend on
scheduling. Background work of a fake (mark echoes, the caller's 20 ms stream, TTS emission) runs in tasks the
fake owns and cancels in :meth:`aclose`.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections import deque
from collections.abc import AsyncIterator, Coroutine, Iterable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray
from text_helpers import Alignment

from va_common.time import Clock
from va_voice.audio import (
    FRAME_MS,
    PCM16LE_16000,
    ULAW_8000,
    AudioFormat,
    L16ByteOrder,
    l16_to_pcm,
    pcm_to_l16,
    rms_dbfs,
    ulaw_decode,
    ulaw_encode,
)
from va_voice.latency import TurnReport
from va_voice.pipeline import (
    AgentTurn,
    BrainAction,
    BrainOutput,
    BrainTiming,
    InboundAudio,
    InboundDtmf,
    MarkEcho,
    TransportEvent,
    UserTurn,
)
from va_voice.vad import WINDOW_MS, VadDecider, VadEvent
from va_voice.vad_model import WINDOW_SAMPLES

__all__ = [
    "LOUD_DBFS",
    "BackgroundTasks",
    "EnergyVad",
    "FakeCaller",
    "FakeStt",
    "FakeSttEvent",
    "FakeTransport",
    "FakeTts",
    "FakeTtsChunk",
    "FakeWord",
    "RecordingActions",
    "RecordingObserver",
    "Said",
    "ScriptedBrain",
    "decode_frame",
    "encode_frame",
    "hanging",
    "tokens",
    "tone",
]

LOUD_DBFS = -40.0
"""Frames/windows above this level count as speech for the energy VAD and the fake STT."""

_TONE_HZ = 440.0
_TONE_AMPLITUDE = 8000.0


class BackgroundTasks:
    """Tasks a fake starts; cancelled and awaited by :meth:`aclose`."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    def spawn(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def aclose(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _now_ms(clock: Clock) -> float:
    return round(clock.monotonic() * 1000, 6)


def tone(samples: int, rate: int, start: int = 0) -> NDArray[np.int16]:
    """A 440 Hz tone (about -12 dBFS) starting at sample ``start``."""
    t = (np.arange(samples) + start) / rate
    return np.rint(_TONE_AMPLITUDE * np.sin(2 * math.pi * _TONE_HZ * t)).astype(np.int16)


def encode_frame(pcm: NDArray[np.int16], fmt: AudioFormat, byte_order: L16ByteOrder = "big") -> bytes:
    if fmt == ULAW_8000:
        return ulaw_encode(pcm)
    return pcm_to_l16(pcm, "little" if fmt == PCM16LE_16000 else byte_order)


def decode_frame(frame: bytes, fmt: AudioFormat, byte_order: L16ByteOrder = "big") -> NDArray[np.int16]:
    if fmt == ULAW_8000:
        return ulaw_decode(frame)
    return l16_to_pcm(frame, "little" if fmt == PCM16LE_16000 else byte_order)


class FakeTransport:
    """Media socket: inbound events from a queue, outbound frames/marks/clears recorded with virtual ms.

    ``echo_delay_ms``: a sent mark comes back this long after it was sent (``None`` = marks are lost).
    """

    def __init__(self, clock: Clock, *, echo_delay_ms: float | None = 60.0) -> None:
        self._clock = clock
        self.echo_delay_ms = echo_delay_ms
        self._inbound: asyncio.Queue[TransportEvent | None] = asyncio.Queue()
        self._tasks = BackgroundTasks()
        self.frames: list[tuple[float, bytes]] = []
        self.marks: list[tuple[float, str]] = []
        self.echoes: list[tuple[float, str]] = []
        self.clears: list[float] = []
        self.calls: list[tuple[str, float]] = []

    def push(self, event: TransportEvent) -> None:
        self._inbound.put_nowait(event)

    def close(self) -> None:
        """The far end closed the stream: ``events()`` ends after the queued events."""
        self._inbound.put_nowait(None)

    async def events(self) -> AsyncIterator[TransportEvent]:
        while True:
            event = await self._inbound.get()
            if event is None:
                return
            yield event

    async def send_audio(self, frame: bytes) -> None:
        self.frames.append((_now_ms(self._clock), frame))

    async def send_mark(self, name: str) -> None:
        now = _now_ms(self._clock)
        self.marks.append((now, name))
        self.calls.append(("transport.mark", now))
        if self.echo_delay_ms is not None:
            self._tasks.spawn(self._echo(name, self.echo_delay_ms))

    async def clear(self) -> None:
        now = _now_ms(self._clock)
        self.clears.append(now)
        self.calls.append(("transport.clear", now))

    async def _echo(self, name: str, delay_ms: float) -> None:
        await self._clock.sleep(delay_ms / 1000)
        self.echoes.append((_now_ms(self._clock), name))
        self.push(MarkEcho(name))

    async def aclose(self) -> None:
        await self._tasks.aclose()


class FakeCaller:
    """The caller's side of the stream: one inbound frame every 20 ms (silence unless :meth:`say` is active).

    Set ``streaming = False`` to push frames by hand (:meth:`send`), e.g. out of order.
    """

    def __init__(
        self, transport: FakeTransport, clock: Clock, fmt: AudioFormat, byte_order: L16ByteOrder
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._fmt = fmt
        self._byte_order = byte_order
        self._loud_left = 0
        self._said: asyncio.Event = asyncio.Event()
        self._samples = 0
        self.next_chunk = 1
        self.streaming = True
        self.sent_pcm: list[NDArray[np.int16]] = []

    def frame(self, *, loud: bool) -> bytes:
        """The next 20 ms frame: a tone when ``loud``, else digital silence."""
        n = self._fmt.frame_samples
        pcm = tone(n, self._fmt.sample_rate, self._samples) if loud else np.zeros(n, dtype=np.int16)
        self._samples += n
        self.sent_pcm.append(pcm)
        return encode_frame(pcm, self._fmt, self._byte_order)

    def send(self, payload: bytes, chunk: int | None = None) -> int:
        """Push one frame with ``chunk`` (default: the next number); returns the chunk number used."""
        number = self.next_chunk if chunk is None else chunk
        self.next_chunk = max(self.next_chunk, number + 1)
        self._transport.push(InboundAudio(number, payload))
        return number

    def press(self, digit: str) -> None:
        self._transport.push(InboundDtmf(digit))

    async def say(self, ms: int) -> None:
        """Speak for ``ms`` (whole frames) and return once the last loud frame went out."""
        self._said = asyncio.Event()
        self._loud_left = ms // FRAME_MS
        await self._said.wait()

    async def run(self) -> None:
        while True:
            if self.streaming:
                loud = self._loud_left > 0
                self.send(self.frame(loud=loud))
                if loud:
                    self._loud_left -= 1
                    if self._loud_left == 0:
                        self._said.set()
            await self._clock.sleep(FRAME_MS / 1000)


class EnergyVad:
    """The real :class:`VadDecider` over 512-sample windows with probability 1/0 from the window level."""

    def __init__(self) -> None:
        self._decider = VadDecider()
        self._pending = np.zeros(0, dtype=np.int16)
        self._windows = 0
        self.fed: list[NDArray[np.int16]] = []

    async def feed(self, pcm16k: NDArray[np.int16]) -> list[VadEvent]:
        self.fed.append(pcm16k)
        pending = np.concatenate([self._pending, pcm16k])
        events: list[VadEvent] = []
        ready = (pending.size // WINDOW_SAMPLES) * WINDOW_SAMPLES
        for start in range(0, ready, WINDOW_SAMPLES):
            level = rms_dbfs(pending[start : start + WINDOW_SAMPLES])
            probability = 1.0 if level > LOUD_DBFS else 0.0
            events.extend(self._decider.step(self._windows * WINDOW_MS, probability, level))
            self._windows += 1
        self._pending = pending[ready:]
        return events


@dataclass(frozen=True, slots=True)
class FakeWord:
    text: str


@dataclass(frozen=True, slots=True)
class FakeSttEvent:
    kind: str
    text: str = ""
    words: tuple[FakeWord, ...] = ()
    language_code: str | None = None
    is_update: bool = False


@dataclass(frozen=True, slots=True)
class Said:
    """One caller utterance as the STT recognises it: the committed ``text`` and the ``partial`` before it."""

    text: str
    partial: str | None = None
    language_code: str | None = "en"


class FakeStt:
    """STT: a partial every ``partial_after`` loud frames, the committed transcript ``commit_latency_s`` after
    each commit (the head of ``utterances``, or an empty transcript when none is left)."""

    def __init__(
        self,
        clock: Clock,
        utterances: Iterable[Said | str] = (),
        *,
        fmt: AudioFormat = ULAW_8000,
        commit_latency_s: float = 0.0,
        partial_after: int = 10,
    ) -> None:
        self._clock = clock
        self.utterances: deque[Said] = deque(u if isinstance(u, Said) else Said(u) for u in utterances)
        self._fmt = fmt
        self.commit_latency_s = commit_latency_s
        self._partial_after = partial_after
        self._loud = 0
        self._out: asyncio.Queue[FakeSttEvent | BaseException | None] = asyncio.Queue()
        self._tasks = BackgroundTasks()
        self.audio: list[bytes] = []
        self.commits: list[float] = []

    async def send_audio(self, audio: bytes) -> None:
        self.audio.append(audio)
        stt_fmt = ULAW_8000 if self._fmt == ULAW_8000 else PCM16LE_16000
        if rms_dbfs(decode_frame(audio, stt_fmt)) <= LOUD_DBFS:
            return
        self._loud += 1
        if self._loud % self._partial_after == 0 and self.utterances:
            head = self.utterances[0]
            self.emit(FakeSttEvent("partial", head.partial if head.partial is not None else head.text))

    async def commit(self) -> None:
        self.commits.append(_now_ms(self._clock))
        said = self.utterances.popleft() if self.utterances else Said("", language_code=None)
        self._loud = 0
        words = tuple(FakeWord(word) for word in said.text.split())
        event = FakeSttEvent("committed", said.text, words, said.language_code)
        if self.commit_latency_s == 0:
            self.emit(event)
        else:
            self._tasks.spawn(self._later(event, self.commit_latency_s))

    async def _later(self, event: FakeSttEvent, delay_s: float) -> None:
        await self._clock.sleep(delay_s)
        self.emit(event)

    def emit(self, event: FakeSttEvent | BaseException) -> None:
        """Deliver an event (an exception is raised by ``events()``)."""
        self._out.put_nowait(event)

    def end(self) -> None:
        """End the event stream."""
        self._out.put_nowait(None)

    async def events(self) -> AsyncIterator[FakeSttEvent]:
        while True:
            item = await self._out.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    async def aclose(self) -> None:
        await self._tasks.aclose()


@dataclass(frozen=True, slots=True)
class FakeTtsChunk:
    context_id: str
    audio: bytes
    alignment: Alignment | None = None
    is_final: bool = False


@dataclass(slots=True)
class _Context:
    jobs: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)
    task: asyncio.Task[None] | None = None
    audio_started: bool = False


class FakeTts:
    """Multi-context TTS: ``ms_per_char`` of audio per character (with exact alignment) for each ``send``, the
    first audio of a context ``ttfa_s`` after its first send, ``is_final`` after ``flush``."""

    def __init__(
        self,
        clock: Clock,
        calls: list[tuple[str, float]],
        *,
        fmt: AudioFormat = ULAW_8000,
        ttfa_s: float = 0.0,
        ms_per_char: int = 5,
    ) -> None:
        self._clock = clock
        self.calls = calls
        self._fmt = fmt
        self.ttfa_s = ttfa_s
        self.ms_per_char = ms_per_char
        self._contexts: dict[str, _Context] = {}
        self._out: asyncio.Queue[FakeTtsChunk | BaseException | None] = asyncio.Queue()
        self._tasks = BackgroundTasks()
        self.started: list[str] = []
        self.sent: list[tuple[str, str, bool]] = []
        self.flushed: list[str] = []
        self.cancelled: list[str] = []

    def _log(self, name: str) -> None:
        self.calls.append((name, _now_ms(self._clock)))

    async def start_context(self, context_id: str) -> None:
        self._log("tts.start_context")
        self.started.append(context_id)
        context = _Context()
        context.task = self._tasks.spawn(self._worker(context_id, context))
        self._contexts[context_id] = context

    async def send(self, context_id: str, text: str, *, flush: bool = False) -> None:
        self._log("tts.send")
        self.sent.append((context_id, text, flush))
        self._contexts[context_id].jobs.put_nowait(text)

    async def flush(self, context_id: str) -> None:
        self._log("tts.flush")
        self.flushed.append(context_id)
        self._contexts[context_id].jobs.put_nowait(None)

    async def cancel(self, context_id: str) -> None:
        self._log("tts.cancel")
        self.cancelled.append(context_id)
        context = self._contexts.get(context_id)
        if context is not None and context.task is not None:
            context.task.cancel()

    def emit(self, item: FakeTtsChunk | BaseException) -> None:
        """Deliver a chunk directly (an exception is raised by ``audio()``)."""
        self._out.put_nowait(item)

    def end(self) -> None:
        """End the audio stream."""
        self._out.put_nowait(None)

    async def audio(self) -> AsyncIterator[FakeTtsChunk]:
        while True:
            item = await self._out.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    async def _worker(self, context_id: str, context: _Context) -> None:
        while True:
            text = await context.jobs.get()
            if text is None:
                self.emit(FakeTtsChunk(context_id, b"", None, is_final=True))
                return
            if not context.audio_started and self.ttfa_s:
                await self._clock.sleep(self.ttfa_s)
            context.audio_started = True
            self.emit(self._chunk(context_id, text))

    def _chunk(self, context_id: str, text: str) -> FakeTtsChunk:
        ms = len(text) * self.ms_per_char
        n_bytes = round(ms * self._fmt.bytes_per_ms)
        audio = bytes([0x55 if self._fmt == ULAW_8000 else 0x01]) * n_bytes
        starts = tuple(i * self.ms_per_char for i in range(len(text)))
        durations = (self.ms_per_char,) * len(text)
        return FakeTtsChunk(context_id, audio, Alignment(tuple(text), starts, durations))

    async def aclose(self) -> None:
        await self._tasks.aclose()


async def tokens(text: str, clock: Clock | None = None, *, first_delay_s: float = 0.0) -> AsyncIterator[str]:
    """``text`` as word deltas (an LLM stream); the first one ``first_delay_s`` after iteration starts."""
    if clock is not None and first_delay_s:
        await clock.sleep(first_delay_s)
    for word in text.split(" "):
        yield f"{word} "


async def hanging(text: str) -> AsyncIterator[str]:
    """An LLM stream that delivers ``text`` and then never finishes (the TTS context stays open)."""
    yield text
    await asyncio.Event().wait()


class ScriptedBrain:
    """A Brain that plays back scripted outputs: ``opener`` once, then one ``replies`` entry per user turn."""

    def __init__(
        self,
        opener: Iterable[BrainOutput] = (),
        replies: Iterable[Iterable[BrainOutput]] = (),
        *,
        timing: BrainTiming | None = None,
    ) -> None:
        self.opener = list(opener)
        self.replies: deque[list[BrainOutput]] = deque(list(reply) for reply in replies)
        self.timing_value = timing if timing is not None else BrainTiming()
        self.user_turns: list[UserTurn] = []
        self.no_input_calls = 0
        self.interrupted: list[str] = []
        self.closed = 0

    async def start(self) -> AsyncIterator[BrainOutput]:
        try:
            for output in self.opener:
                yield output
        finally:
            self.closed += 1

    async def on_user_turn(self, turn: UserTurn) -> AsyncIterator[BrainOutput]:
        self.user_turns.append(turn)
        reply = self.replies.popleft() if self.replies else []
        try:
            for output in reply:
                yield output
        finally:
            self.closed += 1

    async def on_no_input(self) -> AsyncIterator[BrainOutput]:
        self.no_input_calls += 1
        for output in self.replies.popleft() if self.replies else []:
            yield output

    async def on_interrupted(self, spoken_text: str) -> None:
        self.interrupted.append(spoken_text)

    def timing(self) -> BrainTiming:
        return self.timing_value


class RecordingObserver:
    def __init__(self) -> None:
        self.user_turns: list[UserTurn] = []
        self.agent_turns: list[AgentTurn] = []
        self.reports: list[TurnReport] = []

    def on_user_turn(self, turn: UserTurn) -> None:
        self.user_turns.append(turn)

    def on_agent_turn(self, turn: AgentTurn) -> None:
        self.agent_turns.append(turn)

    def on_latency(self, report: TurnReport) -> None:
        self.reports.append(report)


class RecordingActions:
    def __init__(self) -> None:
        self.handled: list[BrainAction] = []

    async def handle(self, action: BrainAction) -> None:
        self.handled.append(action)
