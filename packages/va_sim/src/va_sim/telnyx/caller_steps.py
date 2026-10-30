"""Steps of the FakeTelnyx scenario caller runner (specs/30-testing-and-evals.md §3.5, Decision 2026-10-29
P03-T03c rules 5 to 7).

:class:`ScriptSteps` runs one scenario step at a time against a media session and records its outcome
(``ok``, ``timeout``, ``failed``) with sim-clock start and end times. Every wait is driven by the session's
ticks (and by hang-up and media-end wake-ups) raced against a sim-clock deadline, so steps take exactly the
virtual time the scenario describes. A wait raises :class:`Interrupted` when the leg ends or the media
session stops underneath it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Final, Literal

from va_sim.scenario import (
    Dtmf,
    DtmfStep,
    ExpectHangup,
    ExpectHangupStep,
    HangupStep,
    Say,
    SayStep,
    SilenceStep,
    Step,
    WaitAgentSpeech,
    WaitAgentSpeechStep,
)
from va_sim.telnyx.agent_speech import SpeechDetector
from va_sim.telnyx.media_format import FRAME_MS
from va_sim.telnyx.store import Record

if TYPE_CHECKING:
    from va_sim.state import SimState
    from va_sim.telnyx.calls import Call
    from va_sim.telnyx.media_session import MediaSession

__all__ = [
    "BARGE_IN_GAP_MS",
    "BARGE_IN_TIMEOUT_MS",
    "CALL_ENDED",
    "DTMF_PAUSE_MS",
    "GREETING",
    "MEDIA_STOPPED",
    "ClipKey",
    "Interrupted",
    "ScriptSteps",
]

# A barge-in waits at most this long for enough agent speech; a pause this long ends the agent's turn.
BARGE_IN_TIMEOUT_MS: Final = 10_000
BARGE_IN_GAP_MS: Final = 700
# ``w`` in DTMF digits.
DTMF_PAUSE_MS: Final = 500
# Details of steps cancelled by an interruption.
CALL_ENDED: Final = "call ended"
MEDIA_STOPPED: Final = "media stream stopped"
# Clip key index of the greeting (steps use their index).
GREETING: Final = -1

# (step index or GREETING, run index): one prepared clip.
type ClipKey = tuple[int, int]
type Outcome = tuple[Literal["ok", "timeout", "failed"], str | None]

_OK: Final[Outcome] = ("ok", None)


class Interrupted(Exception):  # noqa: N818 - a control-flow signal, not an error
    """The leg ended or the media session stopped while the script was running."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ScriptSteps:
    """Step execution for one leg's script; :class:`va_sim.telnyx.caller_script.CallerScript` drives it."""

    def __init__(self, state: SimState, call: Call, steps: Sequence[Step]) -> None:
        self._state = state
        self._call = call
        self._steps = tuple(steps)
        self._session: MediaSession | None = None
        self._media_ended = False
        self._wake = asyncio.Event()
        self._detector: SpeechDetector | None = None
        self._clips: dict[ClipKey, bytes] = {}
        self._errors: dict[ClipKey, str] = {}
        self._utterance_ids: dict[ClipKey, int] = {}
        self.step_records: list[Record] = [
            {
                "index": index,
                "type": step.key,
                "status": "pending",
                "started_ms": None,
                "ended_ms": None,
                "detail": None,
                **({"utterance_id": None} if isinstance(step, SayStep) else {}),
            }
            for index, step in enumerate(self._steps)
        ]

    # Waiting.

    def _now(self) -> int:
        return self._state.clock.virtual_ms()

    def _interruption(self) -> str | None:
        if self._call.ended:
            return CALL_ENDED
        if self._media_ended:
            return MEDIA_STOPPED
        return None

    def _check_running(self) -> None:
        reason = self._interruption()
        if reason is not None:
            raise Interrupted(reason)

    def _media(self) -> MediaSession:
        """The session the script was started with; :class:`Interrupted` once it (or the leg) has ended."""
        self._check_running()
        if self._session is None:
            raise Interrupted(MEDIA_STOPPED)
        return self._session

    async def _wait(self, ready: Callable[[], bool], deadline_ms: int | None) -> bool:
        """Wait until ``ready()`` holds (True) or the sim clock reaches ``deadline_ms`` (False), re-checked on
        every tick and wake-up. Raises :class:`Interrupted` when the leg or the media session ends first."""
        timer = None
        if deadline_ms is not None:
            delay_s = max(0, deadline_ms - self._now()) / 1000
            timer = asyncio.create_task(self._state.clock.sleep(delay_s))
        try:
            while not ready():
                self._check_running()
                if timer is not None and timer.done():
                    return False
                self._wake.clear()
                waiter = asyncio.create_task(self._wake.wait())
                await asyncio.wait(
                    {waiter} if timer is None else {waiter, timer}, return_when=asyncio.FIRST_COMPLETED
                )
                waiter.cancel()
            return True
        finally:
            if timer is not None:
                timer.cancel()

    async def _pause(self, ms: int) -> None:
        """Stay quiet for ``ms`` on the sim clock."""
        await self._wait(lambda: False, self._now() + ms)

    def _listen(self, gap_ms: int) -> SpeechDetector:
        """A fresh agent speech detector fed by the ticks from now on (speech before the step is ignored)."""
        self._detector = SpeechDetector(gap_ms)
        return self._detector

    async def _send_audio(self, audio: bytes, utterance: tuple[int | None, str] | None) -> None:
        """Queue caller audio and wait until its last byte has been sent."""
        session = self._media()
        end = session.queue_caller(audio, utterance)
        await self._wait(lambda: session.caller_sent >= end, None)

    async def _send_dtmf(self, digits: str) -> None:
        """Queue ``dtmf`` events and wait until the last one has been sent."""
        session = self._media()
        end = session.queue_dtmf(digits)
        await self._wait(lambda: session.dtmf_sent >= end, None)

    # Steps.

    async def _run_step(self, index: int, step: Step) -> None:
        record = self.step_records[index]
        self._check_running()
        record.update(status="running", started_ms=self._now())
        try:
            status, detail = await self._perform(index, step)
        finally:
            self._detector = None
        record.update(status=status, detail=detail, ended_ms=self._now())

    async def _perform(self, index: int, step: Step) -> Outcome:
        match step:
            case WaitAgentSpeechStep(wait_agent_speech=spec):
                return await self._wait_agent_speech(spec)
            case SayStep(say=spec):
                return await self._say(index, spec)
            case DtmfStep(dtmf=spec):
                return await self._dtmf(index, spec)
            case SilenceStep(silence=spec):
                await self._pause(spec.ms)
                return _OK
            case ExpectHangupStep(expect_hangup=spec):
                return await self._expect_hangup(spec)
            case HangupStep():
                self._state.call_engine.hang_up(self._call, "normal_clearing", self._call.remote_side)
                return _OK

    async def _wait_agent_speech(self, spec: WaitAgentSpeech) -> Outcome:
        detector = self._listen(spec.then_silence_ms)
        need = max(spec.min_ms, FRAME_MS)
        heard = await self._wait(
            lambda: detector.turn_ms >= need and detector.silence_ms >= spec.then_silence_ms,
            self._now() + spec.timeout_ms,
        )
        if heard:
            return _OK
        detail = (
            f"no agent turn of {need} ms followed by {spec.then_silence_ms} ms of silence "
            f"within {spec.timeout_ms} ms"
        )
        return "timeout", detail

    async def _say(self, index: int, spec: Say) -> Outcome:
        key = (index, 0)
        error = self._errors.get(key)
        if error is not None:
            return "failed", error
        if spec.barge_in_after_agent_ms is not None:
            detector = self._listen(BARGE_IN_GAP_MS)
            need = max(spec.barge_in_after_agent_ms, FRAME_MS)
            if not await self._wait(lambda: detector.turn_ms >= need, self._now() + BARGE_IN_TIMEOUT_MS):
                return "timeout", f"the agent did not speak for {need} ms within {BARGE_IN_TIMEOUT_MS} ms"
        utterance_id = self._utterance_ids[key]
        self.step_records[index]["utterance_id"] = utterance_id
        await self._send_audio(self._clips[key], (utterance_id, spec.text))
        return _OK

    async def _dtmf(self, index: int, spec: Dtmf) -> Outcome:
        for run_index, run in enumerate(spec.digits.split("w")):
            if run_index:
                await self._pause(DTMF_PAUSE_MS)
            if not run:
                continue
            if spec.inband:
                await self._send_audio(self._clips[(index, run_index)], None)
            else:
                await self._send_dtmf(run)
        return _OK

    async def _expect_hangup(self, spec: ExpectHangup) -> Outcome:
        call = self._call
        if not await self._wait(lambda: call.ended, self._now() + spec.timeout_ms):
            return "timeout", f"the call did not end within {spec.timeout_ms} ms"
        expected = {"agent": call.platform_side, "caller": call.remote_side}.get(spec.by)
        if expected is not None and call.hangup_source != expected:
            return "failed", f"hung up by {call.hangup_source}"
        return _OK
