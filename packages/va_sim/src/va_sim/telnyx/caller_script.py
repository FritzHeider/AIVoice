"""FakeTelnyx scenario caller runner (specs/30-testing-and-evals.md §3.2 media WS client and §3.5, Decision
2026-10-29 P03-T03c).

One :class:`CallerScript` per leg plays the scenario far end on a media session: the caller role speaks
``greeting_audio`` and then runs ``steps``; a transfer leg (role ``transfer_target``) runs only
``transfer_target.steps``. All audio is prepared up front in one worker-thread call while the accelerated
clock is held, so step timing never depends on how fast the host synthesizes. The script listens to the
session's ticks, records every step outcome for the call summary and timeline, and ends ``done`` or
``cancelled`` (the leg ended or the media session stopped).
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from va_sim.scenario import DtmfStep, SayStep, Step, is_wav_path
from va_sim.tasks import hold_clock
from va_sim.telnyx.caller_audio import render_dtmf_tones, render_text, render_wav
from va_sim.telnyx.caller_steps import GREETING, ClipKey, Interrupted, ScriptSteps
from va_sim.telnyx.media_format import MediaFormat
from va_sim.telnyx.media_session import AgentTick
from va_sim.telnyx.store import Record

if TYPE_CHECKING:
    from va_sim.state import SimState
    from va_sim.telnyx.calls import Call
    from va_sim.telnyx.media_session import MediaSession

__all__ = ["CallerScript", "ScriptRole"]

type ScriptRole = Literal["caller", "transfer_target"]
type Job = tuple[ClipKey, Callable[[], bytes]]


def _render_all(jobs: Sequence[Job]) -> dict[ClipKey, bytes | str]:
    """Run every rendering job (worker thread); a failing job yields its error message."""
    rendered: dict[ClipKey, bytes | str] = {}
    for key, job in jobs:
        try:
            rendered[key] = job()
        except (OSError, ValueError) as exc:
            rendered[key] = str(exc)
    return rendered


class CallerScript(ScriptSteps):
    """The scripted far end of one leg (a media listener of the session it runs on)."""

    def __init__(
        self, state: SimState, call: Call, role: ScriptRole, steps: Sequence[Step], greeting: str | None
    ) -> None:
        super().__init__(state, call, steps)
        self.role: ScriptRole = role
        self.status: Literal["running", "done", "cancelled"] = "running"
        self._greeting = greeting
        self.greeting_record: Record | None = (
            None
            if greeting is None
            else {
                "source": "wav" if is_wav_path(greeting) else "text",
                "status": "pending",
                "utterance_id": None,
                "started_ms": None,
                "ended_ms": None,
                "detail": None,
            }
        )

    def start(self, session: MediaSession) -> None:
        """Run the script on ``session`` in the background."""
        self._session = session
        session.add_listener(self)
        self._state.tasks.spawn(self._run(), name=f"telnyx-script-{self._call.call_control_id}")

    # Media listener and wake-ups (O(1): they only feed the detector and set the event).

    def on_tick(self, tick: AgentTick) -> None:
        if self._detector is not None:
            self._detector.feed(tick.speech)
        self._wake.set()

    def on_media_end(self) -> None:
        self._media_ended = True
        self._wake.set()

    def wake(self) -> None:
        """The leg changed state (hang-up): re-check the current wait."""
        self._wake.set()

    # Running.

    async def _run(self) -> None:
        try:
            await self._prepare()
            await self._greet()
            for index, step in enumerate(self._steps):
                await self._run_step(index, step)
            self.status = "done"
        except Interrupted as stop:
            self._cancel_rest(stop.detail)
            self.status = "cancelled"
        finally:
            if self._session is not None:
                self._session.remove_listener(self)

    async def _prepare(self) -> None:
        """Register every utterance in document order and render all audio in one worker-thread call."""
        fmt = self._media().fmt
        jobs = [*self._greeting_jobs(fmt), *self._step_jobs(fmt)]
        if not jobs:
            return
        name = f"telnyx-script-hold-{self._call.call_control_id}"
        async with hold_clock(self._state.clock, self._state.tasks, name=name):
            rendered = await asyncio.to_thread(_render_all, jobs)
        for key, result in rendered.items():
            if isinstance(result, bytes):
                self._clips[key] = result
            else:
                self._errors[key] = result

    def _greeting_jobs(self, fmt: MediaFormat) -> list[Job]:
        greeting = self._greeting
        if greeting is None:
            return []
        if is_wav_path(greeting):
            return [((GREETING, 0), functools.partial(render_wav, Path(greeting), fmt))]
        return self._text_job((GREETING, 0), greeting, "en", fmt)

    def _step_jobs(self, fmt: MediaFormat) -> list[Job]:
        jobs: list[Job] = []
        for index, step in enumerate(self._steps):
            if isinstance(step, SayStep):
                jobs.extend(self._text_job((index, 0), step.say.text, step.say.language, fmt))
            elif isinstance(step, DtmfStep) and step.dtmf.inband:
                runs = enumerate(step.dtmf.digits.split("w"))
                jobs.extend(
                    ((index, i), functools.partial(render_dtmf_tones, run, fmt)) for i, run in runs if run
                )
        return jobs

    def _text_job(self, key: ClipKey, text: str, language: str, fmt: MediaFormat) -> list[Job]:
        """Register the utterance now (no job when the id space is exhausted: the clip fails)."""
        try:
            utterance_id = self._state.register_utterance(text, language, tuple(text.split()))
        except ValueError as exc:
            self._errors[key] = str(exc)
            return []
        self._utterance_ids[key] = utterance_id
        seed = self._state.utterance_seed(utterance_id)
        return [(key, functools.partial(render_text, utterance_id, text, fmt, seed))]

    async def _greet(self) -> None:
        """Speak the greeting (a failed WAV is recorded and the steps still run)."""
        record, greeting = self.greeting_record, self._greeting
        if record is None or greeting is None:
            return
        key = (GREETING, 0)
        self._check_running()
        record["started_ms"] = self._now()
        error = self._errors.get(key)
        if error is not None:
            record.update(status="failed", detail=error, ended_ms=self._now())
            return
        utterance_id = self._utterance_ids.get(key)
        record.update(status="running", utterance_id=utterance_id)
        label = Path(greeting).name if record["source"] == "wav" else greeting
        await self._send_audio(self._clips[key], (utterance_id, label))
        record.update(status="ok", ended_ms=self._now())

    def _cancel_rest(self, detail: str) -> None:
        now = self._now()
        for record in (self.greeting_record, *self.step_records):
            if record is None:
                continue
            if record["status"] == "running":
                record.update(status="cancelled", detail=detail, ended_ms=now)
            elif record["status"] == "pending":
                record.update(status="cancelled", detail=detail)

    def summary(self) -> Record:
        """The ``script`` object of the call summary and timeline."""
        greeting = self.greeting_record
        return {
            "role": self.role,
            "status": self.status,
            "greeting": None if greeting is None else dict(greeting),
            "steps": [dict(record) for record in self.step_records],
        }
