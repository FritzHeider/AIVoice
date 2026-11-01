"""Agent utterances of one leg (specs/07-media-pipeline.md §8 steps 4-6, §9, Decision P04-T12a).

:class:`Speaker` plays one utterance at a time: text (LLM deltas through the
:class:`~va_voice.text.chunker.ClauseChunker`, or one static clause) → pronunciation guide → the utterance's
alignment timeline and the TTS context (one ``send(..., flush=True)`` per clause, ``flush`` at the end);
TTS audio of the current context → timeline + :class:`~va_voice.outbound.OutboundAudio`; chunks of any other
context are stale and dropped. The utterance is one outbound segment (its mark follows the last frame); it is
complete on that mark's echo, or once the playback estimate passes the end plus the mark-loss grace. An
utterance that is cancelled or fails stops its tracker, drops queued audio locally, cancels its TTS context
and is recorded as interrupted when the caller heard any of it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Final

from va_common.logging import get_logger
from va_common.time import Clock
from va_voice.latency import TurnMark, TurnTimer
from va_voice.outbound import FrameMixer, OutboundAudio
from va_voice.pipeline.models import AgentTurn, PipelineStats, SpeakStatic, SpeakText
from va_voice.pipeline.protocols import PipelineObserver, Transport, TtsChunkLike, TtsStream
from va_voice.pipeline.waits import wait_until_set
from va_voice.playback import (
    LEAD_MS_ESTIMATE,
    MARK_LOSS_GRACE_MS,
    PlaybackTracker,
    truncate_assistant_message,
)
from va_voice.text.chunker import ClauseChunker
from va_voice.text.pronunciation import PronunciationApplier

__all__ = ["Speaker"]

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
    final: asyncio.Event = field(default_factory=asyncio.Event)
    chars: int = 0


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
        self._wake = asyncio.Event()
        self._outbound = OutboundAudio(transport, mixer, clock=clock, on_finished=self._on_finished)
        self._active: _Utterance | None = None

    @property
    def outbound(self) -> OutboundAudio:
        """The leg's outbound clock (the pipeline runs :meth:`OutboundAudio.run`)."""
        return self._outbound

    @property
    def tracker(self) -> PlaybackTracker | None:
        """Playback of the utterance in progress."""
        return None if self._active is None else self._active.tracker

    def on_mark(self, name: str) -> None:
        """A transport mark echo."""
        if self._active is not None and self._active.tracker.on_mark(name):
            self._wake.set()

    def on_tts_chunk(self, chunk: TtsChunkLike) -> None:
        """TTS audio or end of a context; anything but the current open context is stale."""
        utterance = self._active
        if utterance is None or chunk.context_id != utterance.context_id or utterance.final.is_set():
            self._stats.stale_tts_chunks += 1
            return
        if chunk.audio:
            if utterance.timer is not None:
                utterance.timer.mark(TurnMark.TTS_FIRST_AUDIO)
            utterance.tracker.timeline.add_chunk(chunk.audio, chunk.alignment)
            self._outbound.push_audio(chunk.audio)
        if chunk.is_final:
            utterance.final.set()

    async def speak(self, output: SpeakText | SpeakStatic, timer: TurnTimer | None) -> AgentTurn | None:
        """Play ``output`` to completion; ``None`` when it had no text to speak."""
        if self._active is not None:
            message = "an utterance is already playing"
            raise RuntimeError(message)
        utterance: _Utterance | None = None
        try:
            async for clause in self._clauses(output, timer):
                if utterance is None:
                    utterance = await self._open(timer)
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
            self._active = None

    async def preempt(self) -> None:
        """Cut the utterance in progress off at once: stop playback, drop queued audio, clear the far end."""
        utterance = self._active
        if utterance is None or utterance.tracker.stopped:
            return
        utterance.tracker.stop(self._now_ms())
        self._outbound.clear()
        await self._transport.clear()

    async def _clauses(self, output: SpeakText | SpeakStatic, timer: TurnTimer | None) -> AsyncIterator[str]:
        if isinstance(output, SpeakStatic):
            text = output.text.strip()
            if text:
                yield f"{text} "
            return
        if timer is not None:
            timer.mark(TurnMark.LLM_REQUEST, output.llm_request_ms)
        chunker = ClauseChunker()
        async for delta in output.tokens:
            if delta and timer is not None:
                timer.mark(TurnMark.LLM_FIRST_TOKEN)
            for clause in chunker.feed(delta):
                yield clause
        for clause in chunker.flush():
            yield clause

    async def _open(self, timer: TurnTimer | None) -> _Utterance:
        index = self._next_index()
        tracker = PlaybackTracker(index, self._tts_format)
        utterance = _Utterance(index, self._context_ids(index), tracker, timer, self._now_ms())
        self._active = utterance
        self._outbound.start_utterance(tracker)
        await self._tts.start_context(utterance.context_id)
        return utterance

    async def _send_clause(self, utterance: _Utterance, clause: str) -> None:
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
        if not utterance.final.is_set():
            try:
                await self._tts.cancel(utterance.context_id)
            except Exception as exc:
                _log.warning("pipeline_tts_cancel_failed", error_type=type(exc).__name__)
        if tracker.t_first_frame_sent is not None and not tracker.completed(now):
            self._record(utterance, interrupted=True)

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
