"""One leg's turn loop (specs/07-media-pipeline.md §1, §5-§9, §11, §18, §20; Decision P04-T12a).

:class:`CallPipeline.run` runs these tasks in one ``asyncio.TaskGroup``:

- ``inbound``: transport events → reorder → STT + VAD (:class:`~va_voice.pipeline.inbound.InboundPath`) → VAD
  edges to the :class:`~va_voice.endpointing.Endpointer`; mark echoes to the :class:`Speaker`. The end of the
  transport's event stream ends the pipeline (``transport_closed``).
- ``stt``: partials to the endpointer, committed transcripts to the pending commit.
- ``endpoint``: sleeps until the endpointer's deadline (woken by every VAD edge and partial); at
  ``EndOfTurn`` it commits the STT, waits for the committed transcript (``stt_commit_timeout_ms``, else the
  latest partial), and queues a :class:`UserTurn` for the Brain.
- ``replies``: opens the call (§11, Decision P05-T10b): awaits the ``before_opener`` hook (the media
  worker's greeting gate: answered wait, precall DTMF), then with ``wait_for_greeting`` stays silent until
  the first user turn (speech or keypad) or ``greeting_timeout_ms`` and runs ``Brain.start(greeting)`` with
  that turn (``None`` on timeout); the no-input timer is never armed before the opener. Then it runs each
  user turn's Brain outputs in order, one reply task at a time.
- ``tts``: TTS audio to the speaker. ``outbound``: the 20 ms outbound clock.
- ``watchdog``: ``max_duration_message`` at ``max_duration - warning`` (pre-empting any reply, not
  interruptible), then ends the pipeline at ``max_duration`` (``max_duration``).
- ``silence``: the no-input timer (§12, Decision P04-T12c). Armed when the leg goes idle (the reply loop
  waits for work, the agent's last utterance has played, nobody speaks, no turn or digit is pending),
  disarmed by any caller speech or digit. On expiry it queues a reprompt (``Brain.on_no_input``; an empty
  stream speaks the node's ``no_input_prompt`` or ``config.no_input_prompt``) while fewer than
  ``max_reprompts`` were spoken since the last user turn, else ``EndCall(no_input_goodbye,
  "no_input_timeout")``.
- ``ivr`` (``ivr_mode`` only): ends the pipeline with ``ivr_dead_end`` (silently: the reply in progress is
  cut off) when ``ivr_dead_end_s`` pass from the opener without progress (:class:`IvrProgress`: a delivered
  turn with new text, or a telephony action).
- ``takeover``: waits for :meth:`CallPipeline.take_over` (specs/07 §14 Decision P05-T10c): stops accepting
  turns, cuts the reply in progress off, speaks the takeover's message once after its cue (beep event, or
  ``silence_ms`` without caller speech, at most ``max_wait_s``), then ends with the takeover's reason. A
  takeover accepted before the opener skips it.
- ``dtmf``: ends a keypad sequence ``DTMF_SEQUENCE_TIMEOUT_MS`` after its last digit (``#`` ends it at once
  in the inbound task) and routes it as the user turn ``"[DTMF: 1 2 3]"`` with ``dtmf`` set (§13), like a
  speech turn. A digit during interruptible playback (level ≥ 1) barges in first, without the false-barge-in
  resume; ``ignore_button_press`` drops every digit.

Barge-in (§7, §8, Decision P04-T12b): after every inbound frame batch and every STT partial the
:class:`~va_voice.pipeline.barge_in.BargeInMonitor` evaluates the interruption policy while an utterance
plays; an honored barge-in clears the far end, cancels the TTS context, cancels the reply task (LLM stream),
hands the truncated text to ``Brain.on_interrupted`` and arms the false-barge-in resume. A user turn that
ends while a reply is still generating (no audio yet) cancels it and is merged with the turn it answered; one
that ends while a reply is audible is held (dropped when only a backchannel) and delivered after playback.
Telephony actions are never cut short (§7 step 5): an ``ActionHandler.handle`` runs shielded in its own
task, so cancelling the reply (barge-in, concurrency rule, watchdog) stops only the rest of the generation;
the reply loop waits for the action before it starts the next reply. Only the end of the pipeline cancels
an action.

Every task failure ends the pipeline with ``error`` (logged ``pipeline_failed``); the first end reason wins.
The pipeline never hangs up, persists or publishes: :meth:`run` returns a :class:`PipelineResult`, turn
records and latency reports go to the :class:`PipelineObserver`, telephony actions to the
:class:`ActionHandler`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Final

from va_common.logging import get_logger
from va_common.time import Clock
from va_voice.endpointing import EndOfTurn, Endpointer, EndpointerSettings, TurnDiscarded
from va_voice.interrupt import InterruptDecision, effective_level
from va_voice.latency import LatencyTracer, TurnMark, TurnTimer
from va_voice.outbound import FrameMixer
from va_voice.pipeline.barge_in import BargeInMonitor, merge_turns
from va_voice.pipeline.config import PipelineConfig
from va_voice.pipeline.dtmf import DtmfBuffer, DtmfSequence, is_dtmf_digit
from va_voice.pipeline.inbound import InboundPath
from va_voice.pipeline.ivr import IvrProgress
from va_voice.pipeline.models import (
    BrainAction,
    BrainOutput,
    BrainTiming,
    EndCall,
    InterruptRecord,
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
    InboundDtmf,
    MarkEcho,
    PhraseSource,
    PipelineObserver,
    SttEventLike,
    SttStream,
    Transport,
    TtsStream,
    VoiceActivityDetector,
)
from va_voice.pipeline.silence import NoInputTimer
from va_voice.pipeline.speaker import Interruption, Speaker
from va_voice.pipeline.takeover import SpeechCue, Takeover
from va_voice.pipeline.waits import wait_until_set, within
from va_voice.reorder import NULL_OBSERVER as NULL_REORDER_OBSERVER
from va_voice.reorder import ReorderObserver
from va_voice.text.pronunciation import PronunciationApplier
from va_voice.vad import VadEvent

__all__ = ["RESUME_PREFIX", "CallPipeline", "StreamEndedError", "default_context_id"]

_log = get_logger(__name__)

_MS_PER_SECOND: Final = 1000.0
_WAKE_EPSILON_MS: Final = 0.001
RESUME_PREFIX: Final = "Sorry — "
"""Prefix of the false-barge-in resume (§7)."""


class StreamEndedError(RuntimeError):
    """A vendor stream (STT events, TTS audio) ended while the pipeline was still running."""


class _PipelineEnded(Exception):  # noqa: N818 (a control-flow signal, not an error)
    """Raised by the task that ends the pipeline; the task group then cancels the others."""


@dataclass(frozen=True, slots=True)
class _TurnJob:
    turn: UserTurn
    timer: TurnTimer | None


@dataclass(frozen=True, slots=True)
class _ResumeJob:
    text: str


@dataclass(frozen=True, slots=True)
class _SayJob:
    """Speak a static line the call's runtime asked for (:meth:`CallPipeline.say`)."""

    text: str


@dataclass(frozen=True, slots=True)
class _NoInputJob:
    """Speak a reprompt (§12)."""


@dataclass(frozen=True, slots=True)
class _GoodbyeJob:
    """Say ``no_input_goodbye`` and end with ``no_input_timeout`` (§12)."""


type _Job = _TurnJob | _ResumeJob | _SayJob | _NoInputJob | _GoodbyeJob


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
        before_opener: Callable[[], Awaitable[None]] | None = None,
        phrases: PhraseSource | None = None,
        on_underrun: Callable[[], None] | None = None,
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
        self._before_opener = before_opener
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
            phrases=phrases,
            on_underrun=on_underrun,
        )
        self._jobs: asyncio.Queue[_Job] = asyncio.Queue()
        self._endpoint_wake = asyncio.Event()
        self._route_lock = asyncio.Lock()
        self._no_input = NoInputTimer(self._config.no_input_timeout_ms, self._config.max_reprompts)
        self._silence_wake = asyncio.Event()
        self._dtmf = DtmfBuffer()
        self._dtmf_wake = asyncio.Event()
        self._ivr = IvrProgress(self._config.ivr_dead_end_s * _MS_PER_SECOND)
        self._ivr_wake = asyncio.Event()
        self._takeover: Takeover | None = None
        self._takeover_wake = asyncio.Event()
        self._cue = SpeechCue()
        self._cue_wake = asyncio.Event()
        self._opened = False
        self._commit: asyncio.Future[SttEventLike] | None = None
        self._turn_start_ms: float | None = None
        self._turn_index = 0
        self._started = False
        self._start_ms = 0.0
        self._closing = False
        self._reply_task: asyncio.Task[None] | None = None
        self._reply_turn: UserTurn | None = None
        self._reply_timer: TurnTimer | None = None
        self._reply_resume = False
        self._action: asyncio.Task[None] | None = None
        self._timing = BrainTiming()
        self._monitor = BargeInMonitor(on_unhonored=self._on_unhonored)
        self._held: UserTurn | None = None
        self._pending_resume: str | None = None
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

    @property
    def barge_in(self) -> BargeInMonitor:
        return self._monitor

    @property
    def no_input(self) -> NoInputTimer:
        return self._no_input

    @property
    def dtmf(self) -> DtmfBuffer:
        return self._dtmf

    @property
    def ivr(self) -> IvrProgress:
        return self._ivr

    @property
    def speech_cue(self) -> SpeechCue:
        return self._cue

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
        finally:
            await self._abandon_action()
        reason = self._end_reason if self._end_reason is not None else "error"
        outbound = self._speaker.outbound.stats
        _log.info(
            "pipeline_ended",
            end_reason=reason,
            turns=self._turn_index,
            outbound_frames=outbound.frames_sent,
            late_frames=outbound.late_frames,
            max_lateness_ms=round(outbound.max_lateness_ms, 1),
            underruns=outbound.underruns,
        )
        return PipelineResult(reason, self._turn_index, self._stats, self._error)

    def _tasks(self) -> list[tuple[str, Coroutine[Any, Any, None]]]:
        tasks: list[tuple[str, Coroutine[Any, Any, None]]] = [
            ("inbound", self._read_transport()),
            ("stt", self._read_stt()),
            ("tts", self._read_tts()),
            ("outbound", self._speaker.outbound.run()),
            ("endpoint", self._endpoint_loop()),
            ("replies", self._reply_loop()),
            ("watchdog", self._watchdog()),
            ("silence", self._silence_loop()),
            ("dtmf", self._dtmf_loop()),
            ("takeover", self._takeover_loop()),
        ]
        if self._config.ivr_mode:
            tasks.append(("ivr", self._ivr_loop()))
        return tasks

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
                await self._check_barge_in()
            elif isinstance(event, MarkEcho):
                self._speaker.on_mark(event.name)
            elif isinstance(event, InboundDtmf):
                await self._on_dtmf(event.digit)
        self._on_vad(await self._inbound.drain())
        dropped = self._dtmf.clear()
        if dropped:
            _log.info("pipeline_dtmf_dropped", digits=dropped)
        raise self._ended("transport_closed")

    def _on_vad(self, events: list[VadEvent]) -> None:
        edges = [event for event in events if event.kind != "probability"]
        for event in edges:
            self._cue.on_vad(event)
            if event.kind == "speech_start":
                self._disarm_no_input()
                if not self._endpointer.turn_open:
                    self._turn_start_ms = event.at_ms
            self._endpointer.on_vad(event)
            self._monitor.on_vad(event)
        if edges:
            self._endpoint_wake.set()
            self._cue_wake.set()

    async def _read_stt(self) -> None:
        async for event in self._stt.events():
            self._on_stt(event)
            if event.kind == "partial":
                await self._check_barge_in()
        message = "the STT event stream ended"
        raise StreamEndedError(message)

    def _on_stt(self, event: SttEventLike) -> None:
        if event.kind == "partial":
            self._endpointer.on_partial(event.text, self._now_ms())
            if self._endpointer.turn_open:
                self._monitor.on_partial(event.text)
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

    # barge-in

    async def _check_barge_in(self) -> None:
        now = self._now_ms()
        playing = self._speaker.is_playing(now)
        level = self._level() if playing else 0
        decision = self._monitor.evaluate(now, playing=playing, level=level)
        if decision:
            await self._barge_in(decision, now, level, speech_start_ms=self._monitor.speech_started_ms)

    def _level(self) -> int:
        return effective_level(
            call_level=self._config.interruptibility,
            node_level=self._timing.interruptibility,
            block_interruptions=self._config.block_interruptions,
            interruptible=self._speaker.current_interruptible,
        )

    async def _barge_in(
        self,
        decision: InterruptDecision,
        at_ms: float,
        level: int,
        *,
        speech_start_ms: float | None,
        resumable: bool = True,
    ) -> None:
        """§7 steps 2-7: clear + TTS cancel (speaker), cancel the reply (LLM stream), truncate history.

        ``resumable``: arm the false-barge-in resume (speech only; a digit is real input).
        """
        speech_start = speech_start_ms
        timer = self._reply_timer
        interruption = await self._speaker.interrupt(at_ms)
        if interruption is None:
            return
        truncated = interruption.truncated
        if resumable and interruption.resumable and truncated.unspoken:
            self._pending_resume = truncated.unspoken
        if timer is not None:
            timer.mark_interrupted()
        await self._cancel_reply()
        await self._brain.on_interrupted(truncated.history_text)
        self._report_interrupt(
            interruption, decision, at_ms, at_ms if speech_start is None else speech_start, level
        )

    def _report_interrupt(
        self,
        interruption: Interruption,
        decision: InterruptDecision,
        at_ms: float,
        speech_start: float,
        level: int,
    ) -> None:
        truncated = interruption.truncated
        reaction_ms = interruption.clear_ms - speech_start
        record = InterruptRecord(
            turn_index=interruption.utterance_index,
            at_ms=at_ms,
            speech_start_ms=speech_start,
            clear_ms=interruption.clear_ms,
            reaction_ms=reaction_ms,
            clear_delay_ms=interruption.clear_ms - at_ms,
            played_ms=interruption.played_ms,
            played_chars=truncated.played_chars,
            total_chars=truncated.total_chars,
            level=level,
            reason=decision.reason,
        )
        self._stats.barge_ins += 1
        self._latency.barge_in(honored=True, reaction_ms=reaction_ms)
        self._observer.on_interrupt(record)
        _log.info(
            "pipeline_barge_in",
            turn_index=record.turn_index,
            reason=record.reason,
            level=level,
            reaction_ms=round(reaction_ms, 3),
            played_chars=record.played_chars,
            total_chars=record.total_chars,
        )

    def _on_unhonored(self) -> None:
        """Caller speech overlapped playback and ended (or outlived it) without interrupting."""
        self._stats.unhonored_barge_ins += 1
        self._latency.barge_in(honored=False)

    async def _cancel_reply(self) -> None:
        """Cancel the running reply task and wait for it (closes the Brain stream and the LLM tokens)."""
        task = self._reply_task
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.wait({task})

    # turns

    async def _until(self, wake: asyncio.Event, deadline_ms: float | None) -> None:
        """Sleep until ``deadline_ms`` (forever when ``None``) or until ``wake`` is set."""
        now = self._now_ms()
        if deadline_ms is None:
            await wake.wait()
        elif deadline_ms > now:
            await wait_until_set(wake, self._clock, (deadline_ms - now + _WAKE_EPSILON_MS) / _MS_PER_SECOND)

    async def _endpoint_loop(self) -> None:
        while True:
            self._endpoint_wake.clear()
            await self._until(self._endpoint_wake, self._endpointer.deadline_ms())
            result = self._endpointer.poll(self._now_ms())
            if isinstance(result, EndOfTurn):
                await self._end_of_turn(result)
            elif isinstance(result, TurnDiscarded):
                self._on_discarded(result)

    async def _end_of_turn(self, eot: EndOfTurn) -> None:
        start = self._turn_start_ms if self._turn_start_ms is not None else eot.last_voiced_end_ms
        self._turn_start_ms = None
        commit_ms = self._now_ms()
        committed = await self._commit_stt()
        overlapped = self._monitor.overlapped_playback
        self._monitor.reset_turn()
        blank = committed is None or not committed.text.strip()
        if self._pending_resume is not None and blank:
            self._false_barge_in()
            return
        index = self._next_index()
        timer = self._latency.start_turn(index)
        timer.mark(TurnMark.USER_SPEECH_START, start)
        timer.mark(TurnMark.USER_SPEECH_END, eot.last_voiced_end_ms)
        timer.mark(TurnMark.EOT, eot.at_ms)
        timer.mark(TurnMark.STT_COMMIT, commit_ms)
        timer.mark(TurnMark.STT_FINAL)
        await self._route_turn(_user_turn(index, eot, start, committed), timer, overlapped=overlapped)

    def _on_discarded(self, result: TurnDiscarded) -> None:
        self._monitor.reset_turn()
        self._turn_start_ms = None
        if self._pending_resume is not None:
            self._false_barge_in()
            return
        self._stats.discarded_turns += 1
        _log.debug("pipeline_turn_discarded", started_ms=result.started_ms)
        self._flush_held()
        self._arm_no_input()

    def _false_barge_in(self) -> None:
        """§7 false-barge-in protection: noise interrupted the agent; re-speak the unspoken remainder once."""
        remainder = self._pending_resume
        self._pending_resume = None
        if remainder is None or self._closing:
            return
        self._stats.false_barge_ins += 1
        _log.info("pipeline_false_barge_in", chars=len(remainder))
        self._jobs.put_nowait(_ResumeJob(f"{RESUME_PREFIX}{remainder}"))

    async def _route_turn(self, turn: UserTurn, timer: TurnTimer | None, *, overlapped: bool) -> None:
        """§7/§8: deliver, merge with a reply still generating, hold behind audible playback, or drop.

        Serialised: speech turns (endpoint task) and keypad turns (inbound / dtmf tasks) route one at a time.
        """
        async with self._route_lock:
            await self._route_locked(turn, timer, overlapped=overlapped)

    async def _route_locked(self, turn: UserTurn, timer: TurnTimer | None, *, overlapped: bool) -> None:
        if self._closing:
            # Recorded for the transcript, never answered: the call is ending.
            self._stats.user_turns += 1
            self._observer.on_user_turn(turn)
            _log.debug("pipeline_turn_dropped", turn_index=turn.index)
            return
        if overlapped and self._monitor.classifier.is_backchannel(turn.text):
            self._stats.ignored_backchannels += 1
            _log.debug("pipeline_backchannel_ignored", turn_index=turn.index)
            self._flush_held()
            self._arm_no_input()
            return
        running = self._reply_running()
        if running and self._speaker.reply_audio_started:
            self._held = merge_turns(self._held, turn)
            self._stats.held_turns += 1
            _log.debug("pipeline_turn_held", turn_index=turn.index)
            return
        if self._held is not None:
            turn = merge_turns(self._held, turn)
            self._held = None
        if running:
            previous = self._reply_turn
            await self._speaker.cancel_generation()
            await self._cancel_reply()
            turn = merge_turns(previous, turn, supersede=True)
            self._stats.merged_turns += 1
            _log.info("pipeline_turn_merged", turn_index=turn.index, supersedes=turn.supersedes)
        self._deliver(turn, timer)

    def _deliver(self, turn: UserTurn, timer: TurnTimer | None) -> None:
        self._pending_resume = None
        self._no_input.reset()
        self._disarm_no_input()
        self._stats.user_turns += 1
        self._observer.on_user_turn(turn)
        self._jobs.put_nowait(_TurnJob(turn, timer))

    def _flush_held(self) -> None:
        """Deliver the held turn if no reply runs and the caller is not mid-turn (else its EOT merges it)."""
        held = self._held
        if held is None or self._closing or self._reply_running() or self._endpointer.turn_open:
            return
        self._held = None
        self._deliver(held, None)

    def _reply_running(self) -> bool:
        task = self._reply_task
        return task is not None and not task.done()

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
        await self._open_call()
        while True:
            self._flush_held()
            self._arm_no_input()
            job = await self._jobs.get()
            self._disarm_no_input()
            if self._closing:
                _log.debug("pipeline_job_dropped", job=type(job).__name__)
                continue
            await self._run_job(job)
            await self._settle_action()

    async def _open_call(self) -> None:
        """§11: the ``before_opener`` hook, the greeting wait (step 4), then the opener."""
        if self._before_opener is not None:
            await self._before_opener()
        if self._opener_skipped():
            return
        job = await self._await_greeting() if self._config.wait_for_greeting else None
        if self._opener_skipped():
            return
        self._opened = True
        self._ivr.start(self._now_ms())
        self._ivr_wake.set()
        if job is None:
            await self._run_reply(self._brain.start(), None, None)
        else:
            self._note_turn(job.turn)
            await self._run_reply(self._brain.start(job.turn), job.timer, job.turn)
        await self._settle_action()

    def _opener_skipped(self) -> bool:
        """The call started closing (a takeover, the watchdog) before the opener: it is never spoken."""
        if self._closing:
            _log.info("pipeline_opener_skipped")
        return self._closing

    async def _await_greeting(self) -> _TurnJob | None:
        """The first user turn within ``greeting_timeout_ms`` (``None`` on timeout).

        Only turns are queued before the opener (no-input is never armed, resumes need playback).
        """
        getter = asyncio.ensure_future(self._jobs.get())
        if not await within(getter, self._clock, self._config.greeting_timeout_ms / _MS_PER_SECOND):
            self._stats.greeting_timeouts += 1
            _log.info("pipeline_greeting_timeout", timeout_ms=self._config.greeting_timeout_ms)
            return None
        job = getter.result()
        if not isinstance(job, _TurnJob):
            _log.debug("pipeline_job_dropped", job=type(job).__name__)
            return None
        self._stats.greeting_turns += 1
        _log.info("pipeline_greeting_turn", turn_index=job.turn.index)
        return job

    async def _run_job(self, job: _Job) -> None:
        if isinstance(job, _TurnJob):
            self._note_turn(job.turn)
            await self._run_reply(self._brain.on_user_turn(job.turn), job.timer, job.turn)
        elif isinstance(job, _ResumeJob):
            await self._run_reply(_single(SpeakStatic(job.text, interruptible=True)), None, None, resume=True)
        elif isinstance(job, _SayJob):
            await self._run_reply(_single(SpeakStatic(job.text, interruptible=True)), None, None)
        elif isinstance(job, _NoInputJob):
            await self._run_reply(self._no_input_outputs(), None, None)
        else:
            goodbye = EndCall(self._config.no_input_goodbye, "no_input_timeout")
            await self._run_reply(_single(goodbye), None, None)

    async def _run_reply(
        self,
        outputs: AsyncIterator[BrainOutput],
        timer: TurnTimer | None,
        turn: UserTurn | None,
        *,
        resume: bool = False,
    ) -> None:
        """Run one reply in its own task (cancellable on its own); its failure or end signal propagates."""
        self._speaker.begin_reply()
        self._reply_turn = turn
        self._reply_timer = timer
        self._reply_resume = resume
        task = asyncio.get_running_loop().create_task(
            self._guarded("reply", self._reply(outputs, timer)), name="pipeline-reply"
        )
        self._reply_task = task
        try:
            await asyncio.wait({task})
        finally:
            self._reply_task = None
            self._reply_timer = None
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
            await self._speaker.speak(output, timer, resume=self._reply_resume)
        elif isinstance(output, EndCall):
            await self._end_call(output, timer)
        else:
            await self._handle_action(output)

    async def _end_call(self, output: EndCall, timer: TurnTimer | None = None) -> None:
        """§18 step 1: stop accepting outputs, finish the final line (``final_line_timeout_s``), end.

        A final line answering a user turn is that turn's reply: it runs on the turn's ``timer``, so the
        turn's voice-to-voice latency is measured to the goodbye's first audio.
        """
        self._closing = True
        text = (output.final_text or "").strip()
        if text:
            final_line = self._speaker.speak(SpeakStatic(text, interruptible=False), timer)
            if not await within(final_line, self._clock, self._config.final_line_timeout_s):
                _log.warning("pipeline_final_line_timeout", timeout_s=self._config.final_line_timeout_s)
        raise self._ended(output.reason)

    async def _handle_action(self, action: BrainAction) -> None:
        self._ivr.on_action(self._now_ms())
        self._ivr_wake.set()
        if self._actions is None:
            _log.info("pipeline_action_unhandled", kind=action.kind)
            return
        task = asyncio.get_running_loop().create_task(self._actions.handle(action), name="pipeline-action")
        self._action = task
        try:
            # Cancelling the reply lands here at once; the action itself runs to completion.
            await asyncio.shield(task)
        finally:
            if task.done():
                self._action = None

    async def _settle_action(self) -> None:
        """Wait for an action that outlived its cancelled reply; its failure fails the reply loop."""
        task = self._action
        if task is None:
            return
        await asyncio.wait({task})
        self._action = None
        _log.info("pipeline_action_completed_after_cancel")
        task.result()

    async def _abandon_action(self) -> None:
        """The pipeline ended: cancel an action still in flight (the call is over)."""
        task = self._action
        self._action = None
        if task is None:
            return
        if not task.done():
            task.cancel()
            _log.info("pipeline_action_abandoned")
        await asyncio.wait({task})
        if not task.cancelled() and task.exception() is not None:
            exc = task.exception()
            _log.warning("pipeline_action_failed", error_type=type(exc).__name__, error=str(exc))

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

    # runtime lines (§16)

    def say(self, text: str) -> bool:
        """Queue ``text`` as an interruptible agent line after the current reply (and action) finished
        (specs/07 §16 Decision P05-T13: the transfer-failed message); ``False`` when ``text`` is blank or the
        pipeline is ending. Sync, so an action handler may call it while its action still runs."""
        if not text.strip() or self._closing or self._end_reason is not None:
            return False
        self._jobs.put_nowait(_SayJob(text.strip()))
        return True

    # takeover (§14)

    def take_over(self, takeover: Takeover) -> bool:
        """Hand the end of the call to ``takeover`` (specs/07 §14 Decision P05-T10c); ``False`` when a
        takeover was already accepted or the pipeline is already ending (the first end wins). May be called
        before :meth:`run`: the takeover then starts with it and the opener is skipped."""
        if self._takeover is not None or self._closing or self._end_reason is not None:
            return False
        self._takeover = takeover
        self._closing = True
        self._takeover_wake.set()
        _log.info("pipeline_takeover", reason=takeover.reason, message=bool((takeover.text or "").strip()))
        return True

    async def _takeover_loop(self) -> None:
        await self._takeover_wake.wait()
        takeover = self._takeover
        if takeover is None:  # the wake is only set together with the takeover
            return
        self._disarm_no_input()
        dropped = self._dtmf.clear()
        if dropped:
            _log.info("pipeline_dtmf_dropped", digits=dropped)
        await self._stop_reply()
        text = (takeover.text or "").strip()
        if text:
            await self._await_message_cue(takeover)
            message = self._speaker.speak(SpeakStatic(text, interruptible=False), None)
            if not await within(message, self._clock, takeover.speak_timeout_s):
                _log.warning("pipeline_takeover_message_timeout", timeout_s=takeover.speak_timeout_s)
            if takeover.after_message is not None and not await within(
                takeover.after_message(), self._clock, takeover.after_message_timeout_s
            ):
                _log.warning(
                    "pipeline_takeover_after_message_timeout", timeout_s=takeover.after_message_timeout_s
                )
        raise self._ended(takeover.reason)

    async def _await_message_cue(self, takeover: Takeover) -> None:
        """The beep, ``silence_ms`` without caller speech, or ``max_wait_s``, whichever comes first."""
        started = self._now_ms()
        cap_ms = started + takeover.max_wait_s * _MS_PER_SECOND
        beep = takeover.beep
        watcher: asyncio.Task[bool] | None = None
        if beep is not None:
            watcher = asyncio.get_running_loop().create_task(beep.wait(), name="pipeline-beep")
            watcher.add_done_callback(lambda _: self._cue_wake.set())
        try:
            trigger = await self._cue_trigger(started, cap_ms, takeover)
        finally:
            if watcher is not None:
                watcher.cancel()
        _log.info("pipeline_takeover_wait", trigger=trigger, waited_ms=round(self._now_ms() - started, 3))

    async def _cue_trigger(self, started: float, cap_ms: float, takeover: Takeover) -> str:
        while True:
            self._cue_wake.clear()
            if takeover.beep is not None and takeover.beep.is_set():
                return "beep"
            now = self._now_ms()
            silence_at = self._cue.silence_at_ms(started, takeover.silence_ms)
            if silence_at is not None and now >= silence_at:
                return "silence"
            if now >= cap_ms:
                return "cap"
            await self._until(self._cue_wake, cap_ms if silence_at is None else min(cap_ms, silence_at))

    # IVR dead end (specs/05 §9)

    def _note_turn(self, turn: UserTurn) -> None:
        if self._ivr.on_turn(turn.text, self._now_ms()):
            self._ivr_wake.set()

    async def _ivr_loop(self) -> None:
        while True:
            self._ivr_wake.clear()
            await self._until(self._ivr_wake, self._ivr.deadline_ms)
            if self._ivr.due(self._now_ms()):
                break
        _log.info("pipeline_ivr_dead_end", dead_end_s=self._config.ivr_dead_end_s)
        self._closing = True
        await self._stop_reply()
        raise self._ended("ivr_dead_end")

    # silence (§12)

    def _idle(self) -> bool:
        """The call is open and nothing happens on the leg: no reply, job, action, open or held turn, pending
        commit or digit."""
        return not (
            self._closing
            or not self._opened
            or self._reply_running()
            or self._action is not None
            or not self._jobs.empty()
            or self._endpointer.turn_open
            or self._held is not None
            or self._commit is not None
            or self._dtmf.pending
            or self._speaker.is_playing(self._now_ms())
        )

    def _arm_no_input(self) -> None:
        if self._idle() and self._no_input.arm(self._now_ms()):
            self._silence_wake.set()

    def _disarm_no_input(self) -> None:
        if self._no_input.disarm():
            self._silence_wake.set()

    async def _silence_loop(self) -> None:
        while True:
            self._silence_wake.clear()
            await self._until(self._silence_wake, self._no_input.deadline_ms)
            if self._no_input.due(self._now_ms()):
                self._on_no_input_expired()

    def _on_no_input_expired(self) -> None:
        if not self._idle():
            self._no_input.disarm()
            return
        action = self._no_input.expire()
        if action == "reprompt":
            self._stats.no_input_reprompts += 1
            _log.info("pipeline_no_input_reprompt", reprompts=self._no_input.reprompts)
            self._jobs.put_nowait(_NoInputJob())
        else:
            _log.info("pipeline_no_input_timeout", reprompts=self._no_input.reprompts)
            self._jobs.put_nowait(_GoodbyeJob())

    async def _no_input_outputs(self) -> AsyncIterator[BrainOutput]:
        """The Brain's reprompt; when it yields nothing, the node's ``no_input_prompt`` or the default."""
        outputs = self._brain.on_no_input()
        empty = True
        try:
            async for output in outputs:
                empty = False
                yield output
        finally:
            await _close(outputs)
        if empty:
            yield SpeakStatic(
                self._timing.no_input_prompt or self._config.no_input_prompt, interruptible=True
            )

    # DTMF (§13)

    async def _on_dtmf(self, digit: str) -> None:
        if self._config.ignore_button_press:
            self._stats.ignored_dtmf += 1
            _log.debug("pipeline_dtmf_ignored")
            return
        if not is_dtmf_digit(digit):
            _log.warning("pipeline_dtmf_invalid", length=len(digit))
            return
        now = self._now_ms()
        self._disarm_no_input()
        if self._speaker.is_playing(now):
            level = self._level()
            decision = self._monitor.evaluate_dtmf(playing=True, level=level)
            if decision:
                await self._barge_in(decision, now, level, speech_start_ms=now, resumable=False)
        sequence = self._dtmf.press(digit, now)
        self._dtmf_wake.set()
        if sequence is not None:
            await self._route_dtmf(sequence)
        elif not self._dtmf.pending:
            _log.debug("pipeline_dtmf_empty_terminator")
            self._arm_no_input()

    async def _dtmf_loop(self) -> None:
        while True:
            self._dtmf_wake.clear()
            await self._until(self._dtmf_wake, self._dtmf.deadline_ms)
            sequence = self._dtmf.poll(self._now_ms())
            if sequence is not None:
                await self._route_dtmf(sequence)

    async def _route_dtmf(self, sequence: DtmfSequence) -> None:
        """A finished keypad sequence becomes a user turn (no latency timer, never a backchannel)."""
        turn = UserTurn(
            index=self._next_index(),
            text=sequence.text,
            start_ms=sequence.first_ms,
            end_ms=sequence.last_ms,
            dtmf=sequence.digits,
        )
        self._stats.dtmf_turns += 1
        # Never log the digits themselves: they may be PINs or card numbers.
        _log.info("pipeline_dtmf_turn", turn_index=turn.index, digits=len(sequence.digits))
        await self._route_turn(turn, None, overlapped=False)
        self._arm_no_input()

    # helpers

    def _apply_timing(self) -> None:
        """Endpointing settings of the Brain's current node (re-read after every reply)."""
        timing = self._brain.timing()
        self._timing = timing
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


async def _single(output: BrainOutput) -> AsyncIterator[BrainOutput]:
    """One pipeline-made output, no Brain involved (false-barge-in resume, no-input goodbye)."""
    yield output


async def _close(outputs: AsyncIterator[BrainOutput]) -> None:
    """Close a Brain output stream that is an async generator (others have nothing to release)."""
    aclose: Callable[[], Coroutine[Any, Any, None]] | None = getattr(outputs, "aclose", None)
    if aclose is not None:
        await aclose()
