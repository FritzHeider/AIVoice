"""CallPipeline edge cases: STT notices, stray marks, DTMF before T12c, plain iterators, wait helpers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from pipeline_fakes import FakeSttEvent, ScriptedBrain, hanging, tokens
from pipeline_harness import harness
from service_helpers import json_log_stream, log_events

from va_sim.clock import AcceleratedClock
from va_voice.latency import TurnLatency, TurnReport
from va_voice.pipeline import (
    NULL_OBSERVER,
    AgentTurn,
    BrainOutput,
    MarkEcho,
    PipelineConfig,
    SpeakStatic,
    SpeakText,
    UserTurn,
    wait_until_set,
    within,
)


async def test_stt_notices_and_stray_commits_are_logged_and_ignored() -> None:
    stream = json_log_stream()
    async with harness(brain=ScriptedBrain(replies=[[]]), utterances=["Hello there."]) as h:
        h.stt.emit(FakeSttEvent("warning", "quota low"))
        h.stt.emit(FakeSttEvent("error", "transient"))
        h.stt.emit(FakeSttEvent("committed", "late transcript"))
        h.stt.emit(FakeSttEvent("session_started"))
        await h.turn(600)
        h.stt.emit(FakeSttEvent("committed", "Hello there.", is_update=True))
        await h.until(lambda: h.now_ms() >= 2000)
        assert [turn.text for turn in h.brain.user_turns] == ["Hello there."]
    events = log_events(stream)
    notices = [(e["kind"], e["text"]) for e in events if e["event"] == "pipeline_stt_notice"]
    assert notices == [("warning", "quota low"), ("error", "transient")]


async def test_dtmf_and_unknown_marks_do_not_disturb_the_turn() -> None:
    brain = ScriptedBrain(opener=[SpeakStatic("Hi, this is Ava.")])
    async with harness(brain=brain) as h:
        h.caller.press("5")
        h.transport.push(MarkEcho("seg:99:0"))
        h.transport.push(MarkEcho("not-a-mark"))
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        assert h.pipeline.speaker.tracker is None
        assert h.pipeline.config.fmt.name == "ulaw_8000"
        assert h.pipeline.inbound.origin_ms == -20.0


async def test_a_second_utterance_while_one_plays_is_rejected() -> None:
    brain = ScriptedBrain(opener=[SpeakText(hanging("Hello there, how are you doing today? "))])
    async with harness(brain=brain) as h:
        await h.until(lambda: h.pipeline.speaker.tracker is not None)
        with pytest.raises(RuntimeError, match="already playing"):
            await h.pipeline.speaker.speak(SpeakStatic("Hi."), None)


async def test_a_brain_stream_failing_before_any_text_opens_no_context() -> None:
    async def broken() -> AsyncIterator[str]:
        empty: tuple[str, ...] = ()
        for token in empty:
            yield token
        message = "no tokens"
        raise ValueError(message)

    async with harness(brain=ScriptedBrain(opener=[SpeakText(broken())])) as h:
        result = await h.result()
        assert (result.end_reason, h.tts.started, h.tts.cancelled) == ("error", [], [])


async def test_text_without_a_final_boundary_is_flushed() -> None:
    async with harness(brain=ScriptedBrain(opener=[SpeakText(tokens("okay then"))])) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        assert h.tts.sent == [("turn-0", "okay then ", True)]


async def test_max_duration_cancels_a_reply_that_has_not_spoken_yet() -> None:
    config = PipelineConfig(max_duration_s=1.0, max_duration_warning_s=0.5, max_duration_message="Bye.")
    async with harness(brain=ScriptedBrain(opener=[SpeakText(hanging(""))]), config=config) as h:
        result = await h.result()
        assert result.end_reason == "max_duration"
        assert h.transport.clears == []
        assert [turn.text for turn in h.observer.agent_turns] == ["Bye."]


async def test_a_warning_longer_than_the_rest_of_the_call_is_cut_at_max_duration() -> None:
    config = PipelineConfig(max_duration_s=1.0, max_duration_warning_s=0.2)
    async with harness(config=config) as h:
        result = await h.result()
        assert result.end_reason == "max_duration"
        assert h.now_ms() == pytest.approx(1000, abs=1)
        assert h.observer.agent_turns[0].interrupted is True


class _PlainOutputs:
    """A Brain output stream that is an iterator object, not an async generator (no ``aclose``)."""

    def __init__(self, items: list[BrainOutput]) -> None:
        self._items = items

    def __aiter__(self) -> _PlainOutputs:
        return self

    async def __anext__(self) -> BrainOutput:
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _PlainBrain(ScriptedBrain):
    def start(self) -> AsyncIterator[BrainOutput]:
        return _PlainOutputs([SpeakStatic("Hello.")])


async def test_brain_streams_without_aclose_are_supported() -> None:
    async with harness(brain=_PlainBrain()) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        assert h.observer.agent_turns[0].text == "Hello."


def test_null_observer_accepts_everything() -> None:
    NULL_OBSERVER.on_user_turn(UserTurn(0, "hi", 0.0, 1.0))
    NULL_OBSERVER.on_agent_turn(
        AgentTurn(1, "hello", "hello", interrupted=False, started_ms=1.0, ended_ms=2.0)
    )
    NULL_OBSERVER.on_latency(TurnReport(TurnLatency(), []))


async def test_wait_helpers_use_the_injected_clock() -> None:
    clock = AcceleratedClock()
    event = asyncio.Event()
    assert await wait_until_set(event, clock, 0.5) is False
    assert clock.monotonic() == pytest.approx(0.5)
    event.set()
    assert await wait_until_set(event, clock, 0.5) is True
    assert await within(asyncio.sleep(0), clock, None) is True

    async def fails() -> None:
        message = "inner"
        raise KeyError(message)

    with pytest.raises(KeyError):
        await within(fails(), clock, 1.0)
    assert clock.monotonic() == pytest.approx(0.5)
