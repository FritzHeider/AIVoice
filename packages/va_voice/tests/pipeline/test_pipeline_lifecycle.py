"""CallPipeline lifecycle: transport close, EndCall, max duration, failures, actions, cancel (P04-T12a)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from pipeline_fakes import (
    EnergyVad,
    FakeStt,
    FakeTransport,
    FakeTts,
    RecordingActions,
    ScriptedBrain,
    hanging,
)
from pipeline_harness import harness
from service_helpers import json_log_stream, log_events

from va_domain.org import OrgSettings
from va_sim.clock import AcceleratedClock
from va_voice.audio import AudioFormat
from va_voice.pipeline import (
    DEFAULT_MAX_DURATION_MESSAGE,
    BrainAction,
    BrainTiming,
    CallPipeline,
    EndCall,
    PipelineConfig,
    SpeakStatic,
    SpeakText,
    StreamEndedError,
)

LONG = "This is a fairly long sentence that keeps the agent speaking for a while, so the caller can hang up."


async def test_transport_close_ends_the_pipeline_and_cancels_every_task() -> None:
    before = asyncio.all_tasks()
    brain = ScriptedBrain(opener=[SpeakText(hanging(LONG))])
    async with harness(brain=brain) as h:
        await h.until(lambda: len(h.transport.frames) > 10)
        h.transport.close()
        result = await h.result()
        assert result.end_reason == "transport_closed"
        assert result.error is None
        assert result.turns == 1
        assert h.tts.cancelled == ["turn-0"]
        assert len(h.observer.agent_turns) == 1
        spoken = h.observer.agent_turns[0]
        assert spoken.interrupted is True
        assert spoken.text_spoken
        assert LONG.startswith(spoken.text_spoken)
        assert len(spoken.text_spoken) < len(spoken.text)
        assert h.brain.closed == 1
    leftover = {task for task in asyncio.all_tasks() - before if not task.done()}
    assert {task.get_name() for task in leftover} <= {"sim-clock-pump"}


async def test_end_call_plays_the_final_line_then_ends() -> None:
    brain = ScriptedBrain(opener=[EndCall("Thanks for calling. Goodbye.")])
    async with harness(brain=brain) as h:
        result = await h.result()
        assert result.end_reason == "agent_hangup"
        assert [turn.text for turn in h.observer.agent_turns] == ["Thanks for calling. Goodbye."]
        assert h.transport.echoes
        assert h.observer.agent_turns[0].ended_ms >= h.transport.echoes[-1][0]
        assert h.tts.cancelled == []


async def test_end_call_without_text_ends_at_once_with_its_reason() -> None:
    brain = ScriptedBrain(opener=[EndCall(reason="transferred"), SpeakStatic("never spoken")])
    async with harness(brain=brain) as h:
        result = await h.result()
        assert (result.end_reason, result.turns) == ("transferred", 0)
        assert h.now_ms() == 0
        assert h.tts.started == []


async def test_end_call_without_mark_echo_ends_after_the_grace() -> None:
    brain = ScriptedBrain(opener=[EndCall("Goodbye.")])
    async with harness(brain=brain, echo_delay_ms=None) as h:
        result = await h.result()
        assert result.end_reason == "agent_hangup"
        first = h.transport.frames[0][0]
        audio_ms = 20 * len(h.transport.frames)
        # Ends once the estimate passes the end of the audio plus 200 ms (lead 60 ms), not after 8 s.
        assert h.now_ms() == pytest.approx(first + 60 + audio_ms + 200, abs=1)
        assert h.observer.agent_turns[0].interrupted is False


async def test_end_call_final_line_is_bounded() -> None:
    config = PipelineConfig(final_line_timeout_s=0.5)
    brain = ScriptedBrain(opener=[EndCall(LONG * 3)])
    async with harness(brain=brain, config=config) as h:
        stream = json_log_stream()
        result = await h.result()
        assert result.end_reason == "agent_hangup"
        assert h.now_ms() == pytest.approx(500, abs=1)
        assert h.tts.cancelled == []
        assert h.observer.agent_turns[0].interrupted is True
        assert "pipeline_final_line_timeout" in [event["event"] for event in log_events(stream)]


async def test_max_duration_warns_then_ends() -> None:
    config = PipelineConfig(max_duration_s=30.0, max_duration_warning_s=10.0, no_input_timeout_ms=30_000)
    async with harness(config=config) as h:
        result = await h.result()
        assert result.end_reason == "max_duration"
        assert h.now_ms() == pytest.approx(30_000, abs=1)
        warning = h.observer.agent_turns[0]
        assert warning.text == DEFAULT_MAX_DURATION_MESSAGE
        assert warning.started_ms == pytest.approx(20_000, abs=1)
        assert warning.interrupted is False


async def test_max_duration_preempts_a_running_reply() -> None:
    config = PipelineConfig(
        max_duration_s=1.0, max_duration_warning_s=0.5, max_duration_message="Time is up."
    )
    brain = ScriptedBrain(opener=[SpeakText(hanging(LONG)), SpeakStatic("never spoken")])
    async with harness(brain=brain, config=config) as h:
        result = await h.result()
        assert result.end_reason == "max_duration"
        assert h.now_ms() == pytest.approx(1000, abs=1)
        assert h.transport.clears == [pytest.approx(500, abs=1)]
        cut, warning = h.observer.agent_turns
        assert cut.interrupted is True
        assert warning.text == "Time is up."
        assert h.tts.cancelled[0] == "turn-0"
        assert "never spoken" not in [text for _, text, _ in h.tts.sent]


async def test_max_duration_drops_turns_after_the_warning() -> None:
    config = PipelineConfig(max_duration_s=12.0, max_duration_warning_s=11.0)
    async with harness(
        config=config, brain=ScriptedBrain(replies=[[SpeakStatic("Hi.")]]), utterances=["Hi."]
    ) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        await h.caller.say(600)
        result = await h.result()
        assert result.end_reason == "max_duration"
        assert len(h.observer.user_turns) == 1
        assert h.brain.user_turns == []


async def test_max_duration_after_end_call_skips_the_warning() -> None:
    config = PipelineConfig(max_duration_s=1.0, max_duration_warning_s=0.9)
    brain = ScriptedBrain(opener=[EndCall(LONG * 3)])
    async with harness(brain=brain, config=config) as h:
        result = await h.result()
        assert result.end_reason == "max_duration"
        assert [turn.text for turn in h.observer.agent_turns] == [(LONG * 3).strip()]


async def test_a_failing_component_ends_with_error() -> None:
    async with harness() as h:
        stream = json_log_stream()
        h.stt.emit(RuntimeError("stt exploded"))
        result = await h.result()
        assert result.end_reason == "error"
        assert isinstance(result.error, RuntimeError)
        assert str(result.error) == "stt exploded"
        failed = [event for event in log_events(stream) if event["event"] == "pipeline_failed"]
        assert failed[0]["task"] == "stt"
        assert failed[0]["error_type"] == "RuntimeError"


async def test_a_failing_brain_ends_with_error_and_cancels_the_tts_context() -> None:
    async def failing() -> AsyncIterator[str]:
        yield "Hello there, how are you doing today? "
        message = "llm stream broke"
        raise ValueError(message)

    brain = ScriptedBrain(opener=[SpeakText(failing())])
    async with harness(brain=brain) as h:
        result = await h.result()
        assert (result.end_reason, type(result.error)) == ("error", ValueError)
        assert h.tts.cancelled == ["turn-0"]


async def test_tts_cancel_failure_is_logged_not_raised() -> None:
    brain = ScriptedBrain(opener=[SpeakText(hanging(LONG))])
    async with harness(brain=brain) as h:

        async def broken_cancel(context_id: str) -> None:
            message = f"socket gone ({context_id})"
            raise ConnectionError(message)

        h.tts.cancel = broken_cancel  # type: ignore[method-assign]
        await h.until(lambda: len(h.transport.frames) > 5)
        stream = json_log_stream()
        h.transport.close()
        result = await h.result()
        assert result.end_reason == "transport_closed"
        assert "pipeline_tts_cancel_failed" in [event["event"] for event in log_events(stream)]


@pytest.mark.parametrize("stream", ["stt", "tts"])
async def test_a_vendor_stream_that_ends_is_an_error(stream: str) -> None:
    async with harness() as h:
        fake: FakeStt | FakeTts = h.stt if stream == "stt" else h.tts
        fake.end()
        result = await h.result()
        assert result.end_reason == "error"
        assert isinstance(result.error, StreamEndedError)


async def test_actions_go_to_the_handler() -> None:
    actions = RecordingActions()
    brain = ScriptedBrain(opener=[BrainAction("transfer"), SpeakStatic("Done.")])
    async with harness(brain=brain, actions=actions) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        assert [action.kind for action in actions.handled] == ["transfer"]


async def test_actions_without_a_handler_are_logged() -> None:
    brain = ScriptedBrain(opener=[BrainAction("send_sms"), SpeakStatic("Done.")])
    stream = json_log_stream()
    async with harness(brain=brain) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 1)
    unhandled = [event for event in log_events(stream) if event["event"] == "pipeline_action_unhandled"]
    assert unhandled[0]["kind"] == "send_sms"


async def test_run_twice_is_an_error() -> None:
    async with harness() as h:
        await h.until(lambda: h.now_ms() >= 20)
        with pytest.raises(RuntimeError, match="only be called once"):
            await h.pipeline.run()


async def test_cancelling_run_cancels_everything() -> None:
    before = asyncio.all_tasks()
    clock = AcceleratedClock()
    transport = FakeTransport(clock)
    tts = FakeTts(clock, transport.calls)
    pipeline = CallPipeline(
        transport=transport,
        stt=FakeStt(clock),
        tts=tts,
        vad=EnergyVad(),
        brain=ScriptedBrain(opener=[SpeakText(hanging(LONG))]),
        clock=clock,
    )
    task = asyncio.get_running_loop().create_task(pipeline.run())
    while len(transport.frames) < 5:
        await clock.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tts.cancelled == ["turn-0"]
    await transport.aclose()
    await tts.aclose()
    leftover = {t for t in asyncio.all_tasks() - before if not t.done()}
    assert {t.get_name() for t in leftover} <= {"sim-clock-pump"}


async def test_brain_timing_configures_the_endpointer() -> None:
    brain = ScriptedBrain(timing=BrainTiming(resumption_speed=3, collecting_number=True))
    config = PipelineConfig(interruption_threshold_ms=700, ivr_mode=True)
    async with harness(brain=brain, config=config) as h:
        await h.until(lambda: h.now_ms() >= 20)
        settings = h.pipeline.endpointer.settings
        assert (settings.resumption_speed, settings.collecting_number) == (3, True)
        assert (settings.interruption_threshold_ms, settings.ivr_mode) == (700, True)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"fmt": AudioFormat("opus", 48000, 2, 0)}, "unsupported leg format opus"),
        ({"interruptibility": 5}, "interruptibility"),
        ({"interruption_threshold_ms": 10}, "interruption_threshold_ms"),
        ({"max_duration_s": 0}, "max_duration_s must be > 0"),
        ({"max_duration_s": 5, "max_duration_warning_s": 5}, "max_duration_warning_s"),
        ({"stt_commit_timeout_ms": 0}, "stt_commit_timeout_ms"),
        ({"final_line_timeout_s": 0}, "final_line_timeout_s"),
        ({"no_input_timeout_ms": 2999}, "no_input_timeout_ms"),
        ({"max_reprompts": -1}, "max_reprompts"),
        ({"reorder_window": 0}, "reorder_window"),
    ],
)
def test_config_validation(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        PipelineConfig(**kwargs)  # type: ignore[arg-type]


def test_config_defaults_match_the_org_settings() -> None:
    org = OrgSettings()
    config = PipelineConfig()
    assert config.max_duration_message == org.max_duration_message
    assert config.no_input_goodbye == org.no_input_goodbye
    assert config.tts_format.name == "ulaw_8000"
    assert config.wire_byte_order == "big"
