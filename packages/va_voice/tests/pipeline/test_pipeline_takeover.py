"""CallPipeline takeover: the voicemail monitor ends the call (specs/07 §14 Decision P05-T10c)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from pipeline_fakes import RecordingObserver, ScriptedBrain, hanging
from pipeline_harness import Harness, harness

from va_voice.latency import TurnReport
from va_voice.pipeline import (
    AgentTurn,
    CallPipeline,
    EndCall,
    InterruptRecord,
    ObserverFanout,
    PipelineConfig,
    SpeakStatic,
    SpeakText,
    SpeechCue,
    Takeover,
    UserTurn,
)
from va_voice.vad import VadEvent

MESSAGE = "Please call us back."
WINDOW_MS = 60.0
"""A 20 ms harness step plus a few event-loop turns and the mark echo."""


def config(**overrides: float) -> PipelineConfig:
    values: dict[str, float] = {"no_input_timeout_ms": 30_000}
    values.update(overrides)
    return PipelineConfig(**values)  # type: ignore[arg-type]


def times(h: Harness, name: str) -> list[float]:
    return [t for n, t in h.transport.calls if n == name]


def message_contexts(h: Harness) -> list[str]:
    return [context for context, text, _ in h.tts.sent if text == f"{MESSAGE} "]


def message_started_ms(h: Harness) -> float:
    (context,) = message_contexts(h)
    index = h.tts.started.index(context)
    return times(h, "tts.start_context")[index]


@contextlib.asynccontextmanager
async def speaking(h: Harness, ms: int) -> AsyncIterator[asyncio.Task[None]]:
    task = asyncio.get_running_loop().create_task(h.caller.say(ms))
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


OPENER = (
    "Hello, this is the clinic calling about your appointment next week. "
    "I would like to confirm the time and go over a few details with you. "
) * 4


def talking_brain() -> ScriptedBrain:
    """An opener whose TTS context stays open (the LLM stream never finishes)."""
    return ScriptedBrain(opener=[SpeakText(hanging(OPENER))])


async def test_a_takeover_cuts_the_reply_and_speaks_after_the_beep() -> None:
    beep = asyncio.Event()
    async with harness(config=config(), brain=talking_brain(), utterances=["Leave a message."] * 5) as h:
        await h.until(lambda: len(h.transport.frames) > 0)
        async with speaking(h, 30_000):
            await h.until(lambda: h.pipeline.endpointer.turn_open)
            taken_ms = h.now_ms()
            assert h.pipeline.take_over(Takeover("voicemail_left", text=MESSAGE, beep=beep))
            assert not h.pipeline.take_over(Takeover("voicemail_hangup"))
            await h.until(lambda: h.now_ms() >= taken_ms + 2000)
            assert message_contexts(h) == []
            beep_ms = h.now_ms()
            beep.set()
            result = await h.result()
    assert result.end_reason == "voicemail_left"
    opener_context = h.tts.started[0]
    assert opener_context in h.tts.cancelled
    assert h.transport.clears
    assert taken_ms <= h.transport.clears[0] <= taken_ms + WINDOW_MS
    assert beep_ms <= message_started_ms(h) <= beep_ms + WINDOW_MS
    message_frames = [t for t, _ in h.transport.frames if t >= message_started_ms(h)]
    assert message_frames
    # Caller speech during the takeover never reaches the Brain and never barges in on the message.
    assert h.brain.user_turns == []
    assert h.observer.interrupts == []
    assert h.pipeline.stats.barge_ins == 0


async def test_the_message_starts_after_silence_following_the_greeting() -> None:
    async with harness(config=config(), brain=ScriptedBrain(), utterances=["You have reached Dana."]) as h:
        await h.until(lambda: h.now_ms() >= 200)
        speech = asyncio.get_running_loop().create_task(h.caller.say(1500))
        await h.until(lambda: h.pipeline.speech_cue.speaking)
        assert h.pipeline.take_over(Takeover("voicemail_left", text=MESSAGE))
        await speech
        result = await h.result()
    assert result.end_reason == "voicemail_left"
    speech_end = h.pipeline.speech_cue.last_speech_end_ms
    assert speech_end is not None
    assert speech_end >= 1500
    started = message_started_ms(h)
    assert speech_end + 1200 <= started <= speech_end + 1200 + WINDOW_MS


async def test_without_any_speech_the_message_waits_the_silence_from_the_takeover() -> None:
    async with harness(config=config(), brain=ScriptedBrain()) as h:
        await h.until(lambda: h.now_ms() >= 500)
        taken_ms = h.now_ms()
        assert h.pipeline.take_over(Takeover("voicemail_left", text=MESSAGE))
        result = await h.result()
    assert result.end_reason == "voicemail_left"
    assert taken_ms + 1200 <= message_started_ms(h) <= taken_ms + 1200 + WINDOW_MS


async def test_the_wait_is_capped_while_the_caller_keeps_talking() -> None:
    async with (
        harness(config=config(), brain=ScriptedBrain(), utterances=["Hello?"] * 50) as h,
        speaking(h, 60_000),
    ):
        await h.until(lambda: h.pipeline.endpointer.turn_open)
        taken_ms = h.now_ms()
        assert h.pipeline.take_over(Takeover("voicemail_left", text=MESSAGE, max_wait_s=5.0))
        result = await h.result()
    assert result.end_reason == "voicemail_left"
    assert taken_ms + 5000 <= message_started_ms(h) <= taken_ms + 5000 + WINDOW_MS


async def test_a_takeover_without_text_ends_at_once_silently() -> None:
    async with harness(config=config(), brain=talking_brain()) as h:
        await h.until(lambda: len(h.transport.frames) > 0)
        taken_ms = h.now_ms()
        contexts = len(h.tts.started)
        assert h.pipeline.take_over(Takeover("voicemail_hangup", text="   "))
        result = await h.result()
        ended_ms = h.now_ms()
    assert result.end_reason == "voicemail_hangup"
    assert len(h.tts.started) == contexts
    assert ended_ms <= taken_ms + WINDOW_MS
    assert h.tts.started[0] in h.tts.cancelled


async def test_a_takeover_before_the_opener_skips_it() -> None:
    holder: list[CallPipeline] = []

    async def gate() -> None:
        await asyncio.sleep(0)
        assert holder[0].take_over(Takeover("voicemail_left", text=MESSAGE))

    brain = ScriptedBrain(opener=[SpeakStatic("Hi, this is Ava.")])
    async with harness(config=config(), brain=brain, before_opener=gate) as h:
        holder.append(h.pipeline)
        result = await h.result()
    assert result.end_reason == "voicemail_left"
    assert h.brain.greetings == []
    assert times(h, "brain.start") == []
    assert [text for _, text, _ in h.tts.sent] == [f"{MESSAGE} "]


async def test_a_takeover_accepted_before_run_skips_the_opener() -> None:
    brain = ScriptedBrain(opener=[SpeakStatic("Hi, this is Ava.")])
    holder: list[CallPipeline] = []

    async def gate() -> None:
        # The takeover was requested while the gate ran (the AMD verdict arrived during the answer wait).
        assert not holder[0].take_over(Takeover("voicemail_left", text=MESSAGE))

    async with harness(config=config(), brain=brain, before_opener=gate) as h:
        holder.append(h.pipeline)
        assert h.pipeline.take_over(Takeover("fax_detected"))
        result = await h.result()
    assert result.end_reason == "fax_detected"
    assert h.brain.greetings == []
    assert h.tts.sent == []


async def test_a_takeover_is_refused_once_the_call_is_ending() -> None:
    brain = ScriptedBrain(opener=[EndCall("Goodbye now.", "agent_ended")])
    async with harness(config=config(), brain=brain) as h:
        await h.until(lambda: bool(h.tts.sent))
        assert not h.pipeline.take_over(Takeover("voicemail_hangup"))
        result = await h.result()
    assert result.end_reason == "agent_ended"


async def test_the_watchdog_ending_first_wins() -> None:
    cfg = config(max_duration_s=4.0, max_duration_warning_s=1.0)
    async with harness(config=cfg, brain=ScriptedBrain(), utterances=["Hello?"] * 20) as h:
        speech = asyncio.get_running_loop().create_task(h.caller.say(60_000))
        await h.until(lambda: h.pipeline.endpointer.turn_open)
        assert h.pipeline.take_over(Takeover("voicemail_left", text=MESSAGE))
        result = await h.result()
        speech.cancel()
    assert result.end_reason == "max_duration"
    assert message_contexts(h) == []
    # The takeover had already closed the call: no max-duration warning either.
    assert h.tts.sent == []


def test_speech_cue_counts_silence_from_the_last_speech_end() -> None:
    cue = SpeechCue()
    assert cue.silence_at_ms(100, 1200) == 1300
    cue.on_vad(VadEvent("speech_start", 200, 1.0, speech=True))
    assert cue.silence_at_ms(100, 1200) is None
    cue.on_vad(VadEvent("probability", 300, 1.0, speech=True))
    assert cue.speaking
    cue.on_vad(VadEvent("speech_end", 900, 0.0, speech=False))
    assert cue.silence_at_ms(100, 1200) == 2100


class _Log:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls

    def on_user_turn(self, turn: UserTurn) -> None:
        self.calls.append(f"{self.name}.user.{turn.index}")

    def on_agent_turn(self, turn: AgentTurn) -> None:
        self.calls.append(f"{self.name}.agent.{turn.index}")

    def on_latency(self, report: TurnReport) -> None:
        self.calls.append(f"{self.name}.latency.{report.turn_index}")

    def on_interrupt(self, record: InterruptRecord) -> None:
        self.calls.append(f"{self.name}.interrupt.{record.turn_index}")


async def test_observer_fanout_calls_every_observer_in_order() -> None:
    calls: list[str] = []
    recording = RecordingObserver()
    fanout = ObserverFanout(_Log("a", calls), recording, _Log("b", calls))
    async with harness(config=config(), brain=ScriptedBrain(), utterances=["Hello?"]) as h:
        await h.turn(600)
        await h.until(lambda: bool(h.observer.reports))
        turn = h.observer.user_turns[0]
        report = h.observer.reports[0]
    agent = AgentTurn(1, "Hi.", "Hi.", interrupted=False, started_ms=0.0, ended_ms=10.0)
    record = InterruptRecord(1, 5.0, 4.0, 6.0, 2.0, 1.0, 1.0, 1, 3, 1, "words")
    fanout.on_user_turn(turn)
    fanout.on_agent_turn(agent)
    fanout.on_latency(report)
    fanout.on_interrupt(record)
    assert calls == [
        f"a.user.{turn.index}",
        f"b.user.{turn.index}",
        "a.agent.1",
        "b.agent.1",
        f"a.latency.{report.turn_index}",
        f"b.latency.{report.turn_index}",
        "a.interrupt.1",
        "b.interrupt.1",
    ]
    assert recording.user_turns == [turn]
    assert recording.agent_turns == [agent]
    assert recording.reports == [report]
    assert recording.interrupts == [record]
