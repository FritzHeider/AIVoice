"""Agent utterances of one leg (specs/07-media-pipeline.md §8 steps 4-6, §9, Decision P04-T12a).

:class:`Speaker` plays one utterance at a time: text (LLM deltas through the
:class:`~va_voice.text.chunker.ClauseChunker`, or one static clause) → pronunciation guide → the utterance's
alignment timeline and the TTS context (one ``send(..., flush=True)`` per clause, ``flush`` at the end);
TTS audio of the current context → timeline + :class:`~va_voice.outbound.OutboundAudio`; chunks of any other
context are stale and dropped. The utterance is one outbound segment (its mark follows the last frame); it is
complete on that mark's echo, or once the playback estimate passes the end plus the mark-loss grace. An
utterance that is cancelled or fails stops its tracker, drops queued audio locally, cancels its TTS context
and is recorded as interrupted when the caller heard any of it.

Cached phrases (specs/07 §11 step 5, Decision P05-T10d): with a :class:`PhraseSource`, a ``SpeakStatic`` whose
pronounced text is stored in the phrase cache in the leg's TTS format plays from the cache: no TTS context is
opened, the audio goes through the same outbound clock, and the stored alignment fills the timeline. A miss, a
format mismatch, or a failing lookup speaks it through the live socket.

Barge-in (Decision P04-T12b): :meth:`Speaker.interrupt` stops the playing utterance at the decision
instant (tracker, local queue) before its first await, then clears the far end and cancels the TTS context;
the pipeline then cancels the reply task, whose :meth:`Speaker.speak` closes the LLM token stream and records
the interrupted turn. :meth:`Speaker.cancel_generation` is the same cut without ``clear`` for a reply no one
has heard yet.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from va_common.logging import get_logger
from va_common.time import Clock
from va_voice.latency import TurnMark, TurnTimer
from va_voice.outbound import FrameMixer, OutboundAudio
from va_voice.pipeline.models import AgentTurn, PipelineStats, SpeakStatic, SpeakText
from va_voice.pipeline.protocols import PhraseSource, PipelineObserver, Transport, TtsChunkLike, TtsStream
from va_voice.pipeline.waits import wait_until_set
from va_voice.playback import (
    LEAD_MS_ESTIMATE,
    MARK_LOSS_GRACE_MS,
    PlaybackTracker,
    TruncatedMessage,
    truncate_assistant_message,
)
from va_voice.text.chunker import ClauseChunker
from va_voice.text.pronunciation import PronouncedText, PronunciationApplier

if TYPE_CHECKING:
    from va_voice.phrase_cache import CachedPhrase

__all__ = ["Interruption", "Speaker"]

_log = get_logger(__name__)

_MS_PER_SECOND: Final = 1000.0
_WAKE_EPSILON_MS: Final = 0.001
"""Deadline sleeps overshoot by 1 µs so float rounding never leaves the waiter just short of the deadline."""


@dataclass(slots=True, eq=False)
class _Utterance:
    index: int
    context_id: str
    tracker: PlaybackTracker
    timer: TurnTimer | None
    started_ms: float
    interruptible: bool = True
    resume: bool = False
    final: asyncio.Event = field(default_factory=asyncio.Event)
    chars: int = 0
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class Interruption:
    """The utterance a barge-in cut off: what the caller heard and when the far end was cleared."""

    utterance_index: int
    truncated: TruncatedMessage
    played_ms: float
    clear_ms: float
    resumable: bool
    """False for a false-barge-in resume (it is never resumed again)."""


class Speaker:
    """TTS contexts, outbound audio and playback completion of one leg's agent utterances."""

    __slots__ = (
        "_active",
        "_applier",
        "_clock",
        "_context_ids",
        "_next_index",
        "_observer",
        "_outbound",
        "_phrases",
        "_reply_audio",
        "_stats",
        "_transport",
        "_tts",
        "_tts_format",
        "_wake",
    )

    def __init__(  # noqa: PLR0913 (collaborators of one leg, all keyword-only)
        self,
        *,
        transport: Transport,
        tts: TtsStream,
        mixer: FrameMixer,
        clock: Clock,
        applier: PronunciationApplier,
        observer: PipelineObserver,
        stats: PipelineStats,
        next_index: Callable[[], int],
        context_ids: Callable[[int], str],
        phrases: PhraseSource | None = None,
        on_underrun: Callable[[], None] | None = None,
    ) -> None:
        self._transport = transport
        self._tts = tts
        self._tts_format = mixer.format
        self._clock = clock
        self._applier = applier
        self._observer = observer
        self._stats = stats
        self._next_index = next_index
        self._context_ids = context_ids
        self._phrases = phrases
        self._wake = asyncio.Event()
        self._outbound = OutboundAudio(
            transport, mixer, clock=clock, on_finished=self._on_finished, on_underrun=on_underrun
        )
        self._active: _Utterance | None = None
        self._reply_audio = False

    @property
    def outbound(self) -> OutboundAudio:
        """The leg's outbound clock (the pipeline runs :meth:`OutboundAudio.run`)."""
        return self._outbound

    @property
    def tracker(self) -> PlaybackTracker | None:
        """Playback of the utterance in progress."""
        return None if self._active is None else self._active.tracker

    @property
    def current_interruptible(self) -> bool | None:
        """``interruptible`` of the utterance in progress (``None`` when none)."""
        return None if self._active is None else self._active.interruptible

    @property
    def reply_audio_started(self) -> bool:
        """Some audio of the current reply (since :meth:`begin_reply`) went out to the caller."""
        if self._reply_audio:
            return True
        return self._active is not None and self._active.tracker.t_first_frame_sent is not None

    def begin_reply(self) -> None:
        """A new reply starts: :attr:`reply_audio_started` is false until one of its frames goes out."""
        self._reply_audio = False

    def is_playing(self, now_ms: float) -> bool:
        """The caller is hearing an utterance (first frame sent, not stopped, not completed)."""
        utterance = self._active
        return utterance is not None and not utterance.cancelled and utterance.tracker.is_playing(now_ms)

    def on_mark(self, name: str) -> None:
        """A transport mark echo."""
        if self._active is not None and self._active.tracker.on_mark(name):
            self._wake.set()

    def on_tts_chunk(self, chunk: TtsChunkLike) -> None:
        """TTS audio or end of a context; anything but the current open context is stale."""
        utterance = self._active
        if (
            utterance is None
            or chunk.context_id != utterance.context_id
            or utterance.final.is_set()
            or utterance.cancelled
        ):
            self._stats.stale_tts_chunks += 1
            return
        if chunk.audio:
            if utterance.timer is not None:
                utterance.timer.mark(TurnMark.TTS_FIRST_AUDIO)
            utterance.tracker.timeline.add_chunk(chunk.audio, chunk.alignment)
            self._outbound.push_audio(chunk.audio)
        if chunk.is_final:
            utterance.final.set()

    async def speak(
        self, output: SpeakText | SpeakStatic, timer: TurnTimer | None, *, resume: bool = False
    ) -> AgentTurn | None:
        """Play ``output`` to completion; ``None`` when it had no text to speak.

        ``resume``: a false-barge-in resume (§7), never resumed itself.
        """
        if self._active is not None:
            message = "an utterance is already playing"
            raise RuntimeError(message)
        utterance: _Utterance | None = None
        try:
            cached = await self._cached(output)
            if cached is not None:
                utterance = await self._open(timer, output.interruptible, resume=resume, live=False)
                self._play_cached(utterance, *cached)
            else:
                async with contextlib.aclosing(self._clauses(output, timer)) as clauses:
                    async for clause in clauses:
                        if utterance is None:
                            utterance = await self._open(timer, output.interruptible, resume=resume)
                        await self._send_clause(utterance, clause)
                if utterance is None:
                    return None
                await self._tts.flush(utterance.context_id)
            await utterance.final.wait()
            self._outbound.finish_utterance(utterance.chars)
            await self._await_playback(utterance.tracker)
            return self._record(utterance, interrupted=False)
        except BaseException:
            if utterance is not None:
                await self._abandon(utterance)
            raise
        finally:
            if utterance is not None:
                self._mark_first_audio(utterance)
                if utterance.tracker.t_first_frame_sent is not None:
                    self._reply_audio = True
            self._active = None

    async def interrupt(self, at_ms: float) -> Interruption | None:
        """Barge-in at ``at_ms`` (§7 steps 2-3, 6): ``None`` when nothing is playing.

        Playback stops and the local queue is dropped before the first await, so a second trigger in the same
        tick finds nothing playing; then the far end is cleared and the TTS context cancelled.
        """
        utterance = self._active
        if utterance is None or not self.is_playing(at_ms):
            return None
        played_ms = self._cut(utterance, at_ms)
        truncated = truncate_assistant_message(utterance.tracker.timeline, played_ms)
        await self._transport.clear()
        clear_ms = self._now_ms()
        await self._cancel_context(utterance)
        return Interruption(utterance.index, truncated, played_ms, clear_ms, resumable=not utterance.resume)

    async def cancel_generation(self) -> None:
        """Cut an utterance nobody has heard yet (§8 concurrency rule): TTS cancel, no ``clear``."""
        utterance = self._active
        if utterance is None or utterance.cancelled:
            return
        self._cut(utterance, self._now_ms())
        await self._cancel_context(utterance)

    async def preempt(self) -> None:
        """Cut the utterance in progress off at once: stop playback, drop queued audio, clear the far end."""
        utterance = self._active
        if utterance is None or utterance.cancelled:
            return
        self._cut(utterance, self._now_ms())
        await self._transport.clear()
        await self._cancel_context(utterance)

    async def _clauses(self, output: SpeakText | SpeakStatic, timer: TurnTimer | None) -> AsyncGenerator[str]:
        if isinstance(output, SpeakStatic):
            text = output.text.strip()
            if text:
                yield f"{text} "
            return
        if timer is not None:
            timer.mark(TurnMark.LLM_REQUEST, output.llm_request_ms)
        chunker = ClauseChunker()
        try:
            async for delta in output.tokens:
                if delta and timer is not None:
                    timer.mark(TurnMark.LLM_FIRST_TOKEN)
                for clause in chunker.feed(delta):
                    yield clause
        finally:
            await _close_tokens(output.tokens)
        if timer is not None:
            # A reply without text (only tool calls) still waited for the model: its first output is the end
            # of the stream. The first mark wins, so this never moves a text reply's first token.
            timer.mark(TurnMark.LLM_FIRST_TOKEN)
        for clause in chunker.flush():
            yield clause

    async def _cached(self, output: SpeakText | SpeakStatic) -> tuple[PronouncedText, CachedPhrase] | None:
        """The stored phrase of a ``SpeakStatic`` in the TTS format (``None``: speak it live)."""
        if self._phrases is None or not isinstance(output, SpeakStatic):
            return None
        text = output.text.strip()
        if not text:
            return None
        pronounced = self._applier.apply(text)
        try:
            phrase = await self._phrases.lookup(pronounced.text)
        except Exception as exc:
            _log.warning("pipeline_phrase_lookup_failed", error_type=type(exc).__name__)
            return None
        if phrase is None or not phrase.audio:
            return None
        if phrase.fmt != self._tts_format:
            _log.info("pipeline_phrase_format_mismatch", phrase_format=phrase.fmt.name)
            return None
        return pronounced, phrase

    def _play_cached(self, utterance: _Utterance, pronounced: PronouncedText, phrase: CachedPhrase) -> None:
        """The whole phrase at once: timeline clause + chunk, outbound audio, and the context is final."""
        timeline = utterance.tracker.timeline
        timeline.add_clause(pronounced)
        if utterance.timer is not None:
            utterance.timer.mark(TurnMark.TTS_FIRST_TEXT)
            utterance.timer.mark(TurnMark.TTS_FIRST_AUDIO)
        timeline.add_chunk(phrase.audio, phrase.alignment)
        self._outbound.push_audio(phrase.audio)
        utterance.chars = len(pronounced.original)
        utterance.final.set()

    async def _open(
        self, timer: TurnTimer | None, interruptible: bool, *, resume: bool, live: bool = True
    ) -> _Utterance:
        index = self._next_index()
        tracker = PlaybackTracker(index, self._tts_format)
        utterance = _Utterance(
            index, self._context_ids(index), tracker, timer, self._now_ms(), interruptible, resume
        )
        self._active = utterance
        self._outbound.start_utterance(tracker)
        if live:
            await self._tts.start_context(utterance.context_id)
        return utterance

    async def _send_clause(self, utterance: _Utterance, clause: str) -> None:
        if utterance.cancelled:
            return
        pronounced = self._applier.apply(clause)
        utterance.tracker.timeline.add_clause(pronounced)
        if utterance.timer is not None:
            utterance.timer.mark(TurnMark.TTS_FIRST_TEXT)
        await self._tts.send(utterance.context_id, pronounced.text, flush=True)
        utterance.chars += len(clause)

    async def _await_playback(self, tracker: PlaybackTracker) -> None:
        """Until the last mark echo, or the estimate passes the end plus the mark-loss grace."""
        while True:
            now = self._now_ms()
            if tracker.completed(now):
                return
            self._wake.clear()
            timeout_s: float | None = None
            first = tracker.t_first_frame_sent
            if tracker.finished and first is not None:
                end = first + LEAD_MS_ESTIMATE + tracker.total_ms + MARK_LOSS_GRACE_MS
                timeout_s = (end - now + _WAKE_EPSILON_MS) / _MS_PER_SECOND
            await wait_until_set(self._wake, self._clock, timeout_s)

    async def _abandon(self, utterance: _Utterance) -> None:
        """The utterance was cancelled or failed before completing."""
        now = self._now_ms()
        tracker = utterance.tracker
        if not tracker.stopped and not tracker.completed(now):
            tracker.stop(now)
            self._outbound.clear()
        if not utterance.cancelled:
            utterance.cancelled = True
            await self._cancel_context(utterance)
        if tracker.t_first_frame_sent is not None and not tracker.completed(now):
            self._record(utterance, interrupted=True)

    def _cut(self, utterance: _Utterance, at_ms: float) -> float:
        """Stop ``utterance`` at ``at_ms`` and drop its queued audio (synchronous); returns ``played_ms``."""
        utterance.cancelled = True
        played_ms = utterance.tracker.stop(at_ms)
        self._outbound.clear()
        return played_ms

    async def _cancel_context(self, utterance: _Utterance) -> None:
        """Cancel the utterance's TTS context unless ``is_final`` arrived (nothing open at the vendor)."""
        if utterance.final.is_set():
            return
        try:
            await self._tts.cancel(utterance.context_id)
        except Exception as exc:
            _log.warning("pipeline_tts_cancel_failed", error_type=type(exc).__name__)

    def _record(self, utterance: _Utterance, *, interrupted: bool) -> AgentTurn:
        now = self._now_ms()
        tracker = utterance.tracker
        text = tracker.timeline.original_text.strip()
        spoken = text
        if interrupted:
            spoken = truncate_assistant_message(tracker.timeline, tracker.played_ms(now)).text_spoken
        first = tracker.t_first_frame_sent
        started = utterance.started_ms if first is None else first
        turn = AgentTurn(utterance.index, text, spoken, interrupted, started, now)
        self._stats.agent_turns += 1
        self._observer.on_agent_turn(turn)
        return turn

    def _mark_first_audio(self, utterance: _Utterance) -> None:
        first = utterance.tracker.t_first_frame_sent
        if utterance.timer is not None and first is not None:
            utterance.timer.mark(TurnMark.FIRST_AUDIO_OUT, first)

    def _on_finished(self, tracker: PlaybackTracker) -> None:
        """The outbound clock finished ``tracker``: re-check playback completion."""
        del tracker
        self._wake.set()

    def _now_ms(self) -> float:
        return self._clock.monotonic() * _MS_PER_SECOND


async def _close_tokens(tokens: AsyncIterable[str]) -> None:
    """Close an LLM token stream that is an async generator (§7 step 4: closes the HTTP stream)."""
    aclose: Callable[[], Coroutine[Any, Any, None]] | None = getattr(tokens, "aclose", None)
    if aclose is not None:
        await aclose()
