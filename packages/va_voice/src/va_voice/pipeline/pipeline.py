"""One leg's turn loop (specs/07-media-pipeline.md §1, §5-§9, §11, §18, §20; Decision P04-T12a).

:class:`CallPipeline.run` runs these tasks in one ``asyncio.TaskGroup``:

- ``inbound``: transport events → reorder → STT + VAD (:class:`~va_voice.pipeline.inbound.InboundPath`) → VAD
  edges to the :class:`~va_voice.endpointing.Endpointer`; mark echoes to the :class:`Speaker`. The end of the
  transport's event stream ends the pipeline (``transport_closed``).
- ``stt``: partials to the endpointer, committed transcripts to the pending commit.
- ``endpoint``: sleeps until the endpointer's deadline (woken by every VAD edge and partial); at
  ``EndOfTurn`` it commits the STT, waits for the committed transcript (``stt_commit_timeout_ms``, else the
  latest partial), and queues a :class:`UserTurn` for the Brain.
- ``replies``: runs the opener, then each user turn's Brain outputs in order, one reply task at a time.
- ``tts``: TTS audio to the speaker. ``outbound``: the 20 ms outbound clock.
- ``watchdog``: ``max_duration_message`` at ``max_duration - warning`` (pre-empting any reply, not
  interruptible), then ends the pipeline at ``max_duration`` (``max_duration``).

Every task failure ends the pipeline with ``error`` (logged ``pipeline_failed``); the first end reason wins.
The pipeline never hangs up, persists or publishes: :meth:`run` returns a :class:`PipelineResult`, turn
records and latency reports go to the :class:`PipelineObserver`, telephony actions to the
:class:`ActionHandler`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Final

from va_common.logging import get_logger
from va_common.time import Clock
from va_voice.endpointing import EndOfTurn, Endpointer, EndpointerSettings, TurnDiscarded
from va_voice.latency import LatencyTracer, TurnMark, TurnTimer
from va_voice.outbound import FrameMixer
from va_voice.pipeline.config import PipelineConfig
from va_voice.pipeline.inbound import InboundPath
from va_voice.pipeline.models import (
    BrainAction,
    BrainOutput,
    EndCall,
    PipelineResult,
    PipelineStats,
    SpeakStatic,
    SpeakText,
    UserTurn,
)
from va_voice.pipeline.protocols import (
    NULL_OBSERVER,
    ActionHandler,
    Brain,
    InboundAudio,
    MarkEcho,
    PipelineObserver,
    SttEventLike,
    SttStream,
    Transport,
    TtsStream,
    VoiceActivityDetector,
)
from va_voice.pipeline.speaker import Speaker
from va_voice.pipeline.waits import wait_until_set, within
from va_voice.reorder import NULL_OBSERVER as NULL_REORDER_OBSERVER
from va_voice.reorder import ReorderObserver
from va_voice.text.pronunciation import PronunciationApplier
from va_voice.vad import VadEvent

__all__ = ["CallPipeline", "StreamEndedError", "default_context_id"]

_log = get_logger(__name__)

_MS_PER_SECOND: Final = 1000.0
_WAKE_EPSILON_MS: Final = 0.001


class StreamEndedError(RuntimeError):
    """A vendor stream (STT events, TTS audio) ended while the pipeline was still running."""


class _PipelineEnded(Exception):  # noqa: N818 (a control-flow signal, not an error)
    """Raised by the task that ends the pipeline; the task group then cancels the others."""


@dataclass(frozen=True, slots=True)
class _Job:
    turn: UserTurn
    timer: TurnTimer


def default_context_id(index: int) -> str:
    """TTS context id of the utterance with turn number ``index``: ``turn-{index}``."""
    return f"turn-{index}"


class CallPipeline:
    """The transport-agnostic real-time turn loop of one call leg; :meth:`run` once."""

    def __init__(  # noqa: PLR0913 (collaborators of one leg, all keyword-only)
        self,
        *,
        transport: Transport,
        stt: SttStream,
        tts: TtsStream,
        vad: VoiceActivityDetector,
        brain: Brain,
        clock: Clock,
        config: PipelineConfig | None = None,
        mixer: FrameMixer | None = None,
        latency: LatencyTracer | None = None,
        observer: PipelineObserver = NULL_OBSERVER,
        actions: ActionHandler | None = None,
        context_ids: Callable[[int], str] = default_context_id,
        reorder_observer: ReorderObserver = NULL_REORDER_OBSERVER,
    ) -> None:
        self._config = config if config is not None else PipelineConfig()
        self._transport = transport
        self._stt = stt
        self._tts = tts
        self._brain = brain
        self._clock = clock
        self._latency = latency if latency is not None else LatencyTracer(clock)
        self._observer = observer
        self._actions = actions
        self._stats = PipelineStats()
        self._endpointer = Endpointer()
        self._inbound = InboundPath(
            self._config, stt=stt, vad=vad, now_ms=self._now_ms, reorder_observer=reorder_observer
        )
        self._speaker = Speaker(
            transport=transport,
            tts=tts,
            mixer=mixer if mixer is not None else self._default_mixer(),
            clock=clock,
            applier=PronunciationApplier(self._config.pronunciation),
            observer=observer,
            stats=self._stats,
            next_index=self._next_index,
            context_ids=context_ids,
        )
        self._jobs: asyncio.Queue[_Job] = asyncio.Queue()
        self._endpoint_wake = asyncio.Event()
        self._commit: asyncio.Future[SttEventLike] | None = None
        self._turn_start_ms: float | None = None
        self._turn_index = 0
        self._started = False
        self._start_ms = 0.0
        self._closing = False
        self._reply_task: asyncio.Task[None] | None = None
        self._end_reason: str | None = None
        self._error: BaseException | None = None

    @property
    def config(self) -> PipelineConfig:
        return self._config

    @property
    def stats(self) -> PipelineStats:
        return self._stats

    @property
    def endpointer(self) -> Endpointer:
        return self._endpointer

    @property
    def speaker(self) -> Speaker:
        return self._speaker

    @property
    def inbound(self) -> InboundPath:
        return self._inbound

    async def run(self) -> PipelineResult:
        """Run the leg until it ends; cancelling the caller cancels every task and re-raises."""
        if self._started:
            message = "CallPipeline.run() may only be called once"
            raise RuntimeError(message)
        self._started = True
        self._start_ms = self._now_ms()
        self._apply_timing()
        try:
            async with asyncio.TaskGroup() as group:
                for name, work in self._tasks():
                    group.create_task(self._guarded(name, work), name=f"pipeline-{name}")
        except* _PipelineEnded:
            pass
        reason = self._end_reason if self._end_reason is not None else "error"
        _log.info("pipeline_ended", end_reason=reason, turns=self._turn_index)
        return PipelineResult(reason, self._turn_index, self._stats, self._error)

    def _tasks(self) -> list[tuple[str, Coroutine[Any, Any, None]]]:
        return [
            ("inbound", self._read_transport()),
            ("stt", self._read_stt()),
            ("tts", self._read_tts()),
            ("outbound", self._speaker.outbound.run()),
            ("endpoint", self._endpoint_loop()),
            ("replies", self._reply_loop()),
            ("watchdog", self._watchdog()),
        ]

    async def _guarded(self, name: str, work: Coroutine[Any, Any, None]) -> None:
        try:
            await work
        except _PipelineEnded:
            raise
        except Exception as exc:
            _log.error("pipeline_failed", task=name, error_type=type(exc).__name__, error=str(exc))
            raise self._ended("error", exc) from exc

    def _ended(self, reason: str, error: BaseException | None = None) -> _PipelineEnded:
        """The end signal; the first reason (and error) recorded wins."""
        if self._end_reason is None:
            self._end_reason = reason
            self._error = error
        return _PipelineEnded(reason)

    # inbound

    async def _read_transport(self) -> None:
        async for event in self._transport.events():
            if isinstance(event, InboundAudio):
                self._on_vad(await self._inbound.push(event))
            elif isinstance(event, MarkEcho):
                self._speaker.on_mark(event.name)
            else:
                _log.debug("pipeline_dtmf_ignored")
        self._on_vad(await self._inbound.drain())
        raise self._ended("transport_closed")

    def _on_vad(self, events: list[VadEvent]) -> None:
        edges = [event for event in events if event.kind != "probability"]
        for event in edges:
            if event.kind == "speech_start" and not self._endpointer.turn_open:
                self._turn_start_ms = event.at_ms
            self._endpointer.on_vad(event)
        if edges:
            self._endpoint_wake.set()

    async def _read_stt(self) -> None:
        async for event in self._stt.events():
            self._on_stt(event)
        message = "the STT event stream ended"
        raise StreamEndedError(message)

    def _on_stt(self, event: SttEventLike) -> None:
        if event.kind == "partial":
            self._endpointer.on_partial(event.text, self._now_ms())
            self._endpoint_wake.set()
        elif event.kind == "committed":
            self._on_committed(event)
        elif event.kind in {"error", "warning"}:
            _log.warning("pipeline_stt_notice", kind=event.kind, text=event.text)

    def _on_committed(self, event: SttEventLike) -> None:
        if event.is_update:
            return
        pending = self._commit
        if pending is None or pending.done():
            _log.debug("pipeline_stt_commit_unexpected")
            return
        pending.set_result(event)

    async def _read_tts(self) -> None:
        async for chunk in self._tts.audio():
            self._speaker.on_tts_chunk(chunk)
        message = "the TTS audio stream ended"
        raise StreamEndedError(message)

    # turns

    async def _endpoint_loop(self) -> None:
        while True:
            self._endpoint_wake.clear()
            now = self._now_ms()
            deadline = self._endpointer.deadline_ms()
            if deadline is None:
                await self._endpoint_wake.wait()
            elif deadline > now:
                await wait_until_set(
                    self._endpoint_wake, self._clock, (deadline - now + _WAKE_EPSILON_MS) / _MS_PER_SECOND
                )
            result = self._endpointer.poll(self._now_ms())
            if isinstance(result, EndOfTurn):
                await self._end_of_turn(result)
            elif isinstance(result, TurnDiscarded):
                self._stats.discarded_turns += 1
                _log.debug("pipeline_turn_discarded", started_ms=result.started_ms)

    async def _end_of_turn(self, eot: EndOfTurn) -> None:
        index = self._next_index()
        start = self._turn_start_ms if self._turn_start_ms is not None else eot.last_voiced_end_ms
        self._turn_start_ms = None
        timer = self._latency.start_turn(index)
        timer.mark(TurnMark.USER_SPEECH_START, start)
        timer.mark(TurnMark.USER_SPEECH_END, eot.last_voiced_end_ms)
        timer.mark(TurnMark.EOT, eot.at_ms)
        timer.mark(TurnMark.STT_COMMIT)
        committed = await self._commit_stt()
        timer.mark(TurnMark.STT_FINAL)
        turn = _user_turn(index, eot, start, committed)
        self._stats.user_turns += 1
        self._observer.on_user_turn(turn)
        self._jobs.put_nowait(_Job(turn, timer))

    async def _commit_stt(self) -> SttEventLike | None:
        """Commit the STT and wait for its committed transcript (``None`` after ``stt_commit_timeout_ms``)."""
        pending: asyncio.Future[SttEventLike] = asyncio.get_running_loop().create_future()
        self._commit = pending
        try:
            await self._stt.commit()
            timeout_s = self._config.stt_commit_timeout_ms / _MS_PER_SECOND
            if await within(pending, self._clock, timeout_s):
                return pending.result()
        finally:
            self._commit = None
        self._stats.commit_timeouts += 1
        _log.warning("stt_commit_timeout", timeout_ms=self._config.stt_commit_timeout_ms)
        return None

    # replies

    async def _reply_loop(self) -> None:
        await self._run_reply(self._brain.start(), None)
        while True:
            job = await self._jobs.get()
            if self._closing:
                _log.debug("pipeline_turn_dropped", turn_index=job.turn.index)
                continue
            await self._run_reply(self._brain.on_user_turn(job.turn), job.timer)

    async def _run_reply(self, outputs: AsyncIterator[BrainOutput], timer: TurnTimer | None) -> None:
        """Run one reply in its own task (cancellable on its own); its failure or end signal propagates."""
        task = asyncio.get_running_loop().create_task(
            self._guarded("reply", self._reply(outputs, timer)), name="pipeline-reply"
        )
        self._reply_task = task
        try:
            await asyncio.wait({task})
        finally:
            self._reply_task = None
            if not task.done():
                task.cancel()
                await asyncio.wait({task})
        if not task.cancelled():
            task.result()
        self._apply_timing()

    async def _reply(self, outputs: AsyncIterator[BrainOutput], timer: TurnTimer | None) -> None:
        try:
            async for output in outputs:
                await self._handle_output(output, timer)
        finally:
            await _close(outputs)
            if timer is not None:
                self._observer.on_latency(self._latency.finish(timer))

    async def _handle_output(self, output: BrainOutput, timer: TurnTimer | None) -> None:
        if isinstance(output, SpeakText | SpeakStatic):
            await self._speaker.speak(output, timer)
        elif isinstance(output, EndCall):
            await self._end_call(output)
        else:
            await self._handle_action(output)

    async def _end_call(self, output: EndCall) -> None:
        """§18 step 1: stop accepting outputs, finish the final line (``final_line_timeout_s``), end."""
        self._closing = True
        text = (output.final_text or "").strip()
        if text:
            final_line = self._speaker.speak(SpeakStatic(text, interruptible=False), None)
            if not await within(final_line, self._clock, self._config.final_line_timeout_s):
                _log.warning("pipeline_final_line_timeout", timeout_s=self._config.final_line_timeout_s)
        raise self._ended(output.reason)

    async def _handle_action(self, action: BrainAction) -> None:
        if self._actions is None:
            _log.info("pipeline_action_unhandled", kind=action.kind)
            return
        await self._actions.handle(action)

    async def _stop_reply(self) -> None:
        """Cut off the reply in progress (its audio and its Brain stream)."""
        task = self._reply_task
        if task is None or task.done():
            return
        await self._speaker.preempt()
        task.cancel()
        await asyncio.wait({task})

    async def _watchdog(self) -> None:
        """§18 ``duration_watchdog``: warning at ``max_duration - warning``, end at ``max_duration``."""
        config = self._config
        await self._clock.sleep(config.max_duration_s - config.max_duration_warning_s)
        end_ms = self._start_ms + config.max_duration_s * _MS_PER_SECOND
        if not self._closing:
            self._closing = True
            await self._stop_reply()
            warning = self._speaker.speak(SpeakStatic(config.max_duration_message, interruptible=False), None)
            await within(warning, self._clock, (end_ms - self._now_ms()) / _MS_PER_SECOND)
        remaining_ms = end_ms - self._now_ms()
        if remaining_ms > 0:
            await self._clock.sleep(remaining_ms / _MS_PER_SECOND)
        raise self._ended("max_duration")

    # helpers

    def _apply_timing(self) -> None:
        """Endpointing settings of the Brain's current node (re-read after every reply)."""
        timing = self._brain.timing()
        self._endpointer.configure(
            EndpointerSettings(
                resumption_speed=timing.resumption_speed,
                interruption_threshold_ms=self._config.interruption_threshold_ms,
                ivr_mode=self._config.ivr_mode,
                collecting_number=timing.collecting_number,
            )
        )

    def _default_mixer(self) -> FrameMixer:
        return FrameMixer(self._config.tts_format, wire_byte_order=self._config.wire_byte_order)

    def _next_index(self) -> int:
        index = self._turn_index
        self._turn_index += 1
        return index

    def _now_ms(self) -> float:
        return self._clock.monotonic() * _MS_PER_SECOND


def _user_turn(index: int, eot: EndOfTurn, start_ms: float, committed: SttEventLike | None) -> UserTurn:
    """The committed transcript, or the endpointer's latest partial when none (or a blank one) arrived."""
    if committed is None or not committed.text.strip():
        return UserTurn(index, eot.text.strip(), start_ms, eot.last_voiced_end_ms)
    words = tuple(word.text for word in committed.words)
    return UserTurn(
        index, committed.text.strip(), start_ms, eot.last_voiced_end_ms, words, committed.language_code
    )


async def _close(outputs: AsyncIterator[BrainOutput]) -> None:
    """Close a Brain output stream that is an async generator (others have nothing to release)."""
    aclose: Callable[[], Coroutine[Any, Any, None]] | None = getattr(outputs, "aclose", None)
    if aclose is not None:
        await aclose()
