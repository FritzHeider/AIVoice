"""Deterministic in-memory harness for :class:`va_voice.pipeline.CallPipeline` (Decision P04-T12a).

``async with harness(...) as h:`` builds the fakes of :mod:`pipeline_fakes` on one
:class:`va_sim.clock.AcceleratedClock`, starts ``pipeline.run()`` and the caller's 20 ms inbound stream, and
on exit closes the transport, awaits the run and cancels every fake's background task.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass

from pipeline_fakes import (
    EnergyVad,
    FakeCaller,
    FakeStt,
    FakeTransport,
    FakeTts,
    RecordingObserver,
    Said,
    ScriptedBrain,
    SlowActions,
)

from va_sim.clock import AcceleratedClock
from va_voice.latency import LatencyTracer
from va_voice.pipeline import ActionHandler, CallPipeline, PipelineConfig, PipelineResult

__all__ = ["Harness", "harness"]

_STEP_S = 0.02


@dataclass(slots=True)
class Harness:
    clock: AcceleratedClock
    transport: FakeTransport
    caller: FakeCaller
    stt: FakeStt
    tts: FakeTts
    vad: EnergyVad
    brain: ScriptedBrain
    observer: RecordingObserver
    pipeline: CallPipeline
    task: asyncio.Task[PipelineResult]

    def now_ms(self) -> float:
        return round(self.clock.monotonic() * 1000, 6)

    async def result(self) -> PipelineResult:
        return await self.task

    async def until(self, predicate: Callable[[], bool], timeout_s: float = 30.0) -> None:
        """Advance virtual time in 20 ms steps until ``predicate()`` holds (fails after ``timeout_s``)."""
        end = self.clock.monotonic() + timeout_s
        while not predicate():
            if self.clock.monotonic() > end:
                message = f"condition not met within {timeout_s} s of virtual time"
                raise AssertionError(message)
            if self.task.done():
                self.task.result()
                message = "the pipeline ended before the condition was met"
                raise AssertionError(message)
            await self.clock.sleep(_STEP_S)

    async def turn(self, speech_ms: int = 600) -> None:
        """The caller speaks ``speech_ms`` and the Brain receives the turn."""
        expected = len(self.brain.user_turns) + 1
        await self.caller.say(speech_ms)
        await self.until(lambda: len(self.brain.user_turns) >= expected)


@contextlib.asynccontextmanager
async def harness(  # noqa: PLR0913 (test knobs, all keyword-only)
    *,
    config: PipelineConfig | None = None,
    brain: ScriptedBrain | None = None,
    utterances: Iterable[Said | str] = (),
    echo_delay_ms: float | None = 60.0,
    ttfa_s: float = 0.0,
    commit_latency_s: float = 0.0,
    actions: ActionHandler | None = None,
    latency: Callable[[AcceleratedClock], LatencyTracer] | None = None,
    streaming: bool = True,
    partial_after: int = 10,
) -> AsyncIterator[Harness]:
    cfg = config if config is not None else PipelineConfig()
    clock = AcceleratedClock()
    transport = FakeTransport(clock, echo_delay_ms=echo_delay_ms)
    caller = FakeCaller(transport, clock, cfg.fmt, cfg.wire_byte_order)
    caller.streaming = streaming
    stt = FakeStt(
        clock, utterances, fmt=cfg.fmt, commit_latency_s=commit_latency_s, partial_after=partial_after
    )
    tts = FakeTts(clock, transport.calls, fmt=cfg.tts_format, ttfa_s=ttfa_s)
    vad = EnergyVad()
    the_brain = brain if brain is not None else ScriptedBrain()
    the_brain.bind(clock, transport.calls)
    if isinstance(actions, SlowActions):
        actions.bind(clock)
    observer = RecordingObserver()
    pipeline = CallPipeline(
        transport=transport,
        stt=stt,
        tts=tts,
        vad=vad,
        brain=the_brain,
        clock=clock,
        config=cfg,
        latency=None if latency is None else latency(clock),
        observer=observer,
        actions=actions,
    )
    task = asyncio.get_running_loop().create_task(pipeline.run(), name="harness-pipeline")
    caller_task = asyncio.get_running_loop().create_task(caller.run(), name="harness-caller")
    h = Harness(clock, transport, caller, stt, tts, vad, the_brain, observer, pipeline, task)
    try:
        yield h
    finally:
        caller_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await caller_task
        if not task.done():
            transport.close()
        try:
            await task
        finally:
            for fake in (transport, stt, tts):
                await fake.aclose()
