"""CallPipeline no-input policy: reprompts, goodbye, count reset, timer arming (specs/07 §12, P04-T12c)."""

from __future__ import annotations

import pytest
from pipeline_fakes import Said, ScriptedBrain, hanging, tokens
from pipeline_harness import Harness, harness
from service_helpers import json_log_stream, log_events

from va_voice.pipeline import (
    DEFAULT_NO_INPUT_GOODBYE,
    DEFAULT_NO_INPUT_PROMPT,
    BrainTiming,
    EndCall,
    PipelineConfig,
    SpeakStatic,
    SpeakText,
)

TIMEOUT_MS = 3000
FAST = PipelineConfig(no_input_timeout_ms=TIMEOUT_MS)
WINDOW_MS = 40.0
"""Arming follows the mark echo by at most a couple of event-loop turns and one 20 ms harness step."""


def hi() -> ScriptedBrain:
    return ScriptedBrain(opener=[SpeakStatic("Hi.")])


def texts(h: Harness) -> list[str]:
    return [turn.text for turn in h.observer.agent_turns]


def times(h: Harness, name: str) -> list[float]:
    return [t for n, t in h.transport.calls if n == name]


async def test_silence_reprompts_twice_then_says_goodbye() -> None:
    async with harness(config=FAST, brain=hi()) as h:
        result = await h.result()
    assert result.end_reason == "no_input_timeout"
    assert texts(h) == ["Hi.", DEFAULT_NO_INPUT_PROMPT, DEFAULT_NO_INPUT_PROMPT, DEFAULT_NO_INPUT_GOODBYE]
    assert not any(turn.interrupted for turn in h.observer.agent_turns)
    assert (result.stats.no_input_reprompts, h.brain.no_input_calls) == (2, 2)
    # Each expiry comes TIMEOUT_MS after the previous utterance finished playing.
    turns = h.observer.agent_turns
    expiries = [*times(h, "brain.no_input"), turns[3].started_ms]
    for previous, expiry in zip(turns, expiries, strict=False):
        assert TIMEOUT_MS <= expiry - previous.ended_ms <= TIMEOUT_MS + WINDOW_MS


async def test_reprompts_come_from_the_brain_then_the_node_prompt() -> None:
    brain = ScriptedBrain(
        opener=[SpeakStatic("Hi.")],
        no_input=[[SpeakStatic("Hello? Anyone there?")]],
        timing=BrainTiming(no_input_prompt="Still with me?"),
    )
    config = PipelineConfig(no_input_timeout_ms=TIMEOUT_MS, no_input_goodbye="Bye for now.")
    async with harness(config=config, brain=brain) as h:
        result = await h.result()
    assert texts(h) == ["Hi.", "Hello? Anyone there?", "Still with me?", "Bye for now."]
    assert result.end_reason == "no_input_timeout"


async def test_a_user_turn_resets_the_reprompt_count() -> None:
    brain = ScriptedBrain(opener=[SpeakStatic("Hi.")], replies=[[SpeakStatic("Okay.")]])
    config = PipelineConfig(no_input_timeout_ms=TIMEOUT_MS, max_reprompts=1)
    async with harness(config=config, brain=brain, utterances=[Said("I am here")]) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 2)
        assert h.pipeline.no_input.reprompts == 1
        await h.turn(600)
        assert h.pipeline.no_input.reprompts == 0
        result = await h.result()
    assert texts(h) == [
        "Hi.",
        DEFAULT_NO_INPUT_PROMPT,
        "Okay.",
        DEFAULT_NO_INPUT_PROMPT,
        DEFAULT_NO_INPUT_GOODBYE,
    ]
    assert result.stats.no_input_reprompts == 2


async def test_noise_postpones_the_timer_without_resetting_the_count() -> None:
    config = PipelineConfig(no_input_timeout_ms=TIMEOUT_MS, max_reprompts=1)
    async with harness(config=config, brain=hi()) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 2)
        reprompt_end = h.observer.agent_turns[1].ended_ms
        await h.until(lambda: h.now_ms() >= reprompt_end + 2000)
        await h.caller.say(200)
        await h.until(lambda: h.pipeline.stats.discarded_turns == 1)
        discarded_at = h.now_ms()
        result = await h.result()
    goodbye = h.observer.agent_turns[2]
    assert goodbye.text == DEFAULT_NO_INPUT_GOODBYE
    # The original deadline (reprompt end + TIMEOUT_MS) passed silently; the timer re-armed at the discard
    # (``discarded_at`` is the first 20 ms harness step after it).
    assert goodbye.started_ms > reprompt_end + 2000 + TIMEOUT_MS
    assert TIMEOUT_MS - 20 <= goodbye.started_ms - discarded_at <= TIMEOUT_MS + WINDOW_MS
    assert (result.end_reason, result.stats.no_input_reprompts) == ("no_input_timeout", 1)


async def test_the_timer_waits_for_a_slow_reply_and_an_open_turn() -> None:
    brain = ScriptedBrain(opener=[SpeakStatic("Hi.")], replies=[[SpeakStatic("Done.")]])
    async with harness(config=FAST, brain=brain, utterances=[Said("tell me more")]) as h:
        # The opener's LLM stream takes 5 s (longer than the timeout) to deliver its second word.
        h.brain.opener.insert(0, SpeakText(tokens("Hello there", h.clock, per_token_delay_s=5.0)))
        await h.until(lambda: len(h.observer.agent_turns) == 2)
        assert h.brain.no_input_calls == 0
        # The caller speaks for 4 s: no expiry mid-turn.
        await h.caller.say(4000)
        await h.until(lambda: len(h.observer.agent_turns) == 3)
        assert h.brain.no_input_calls == 0
        await h.until(lambda: h.brain.no_input_calls == 1)
        assert h.now_ms() - h.observer.agent_turns[2].ended_ms <= TIMEOUT_MS + WINDOW_MS


async def test_no_reprompt_allowed_says_goodbye_at_the_first_expiry() -> None:
    config = PipelineConfig(no_input_timeout_ms=TIMEOUT_MS, max_reprompts=0)
    async with harness(config=config, brain=hi()) as h:
        result = await h.result()
    assert texts(h) == ["Hi.", DEFAULT_NO_INPUT_GOODBYE]
    assert (result.end_reason, result.stats.no_input_reprompts, h.brain.no_input_calls) == (
        "no_input_timeout",
        0,
        0,
    )


async def test_no_timer_while_the_call_is_closing() -> None:
    brain = ScriptedBrain(opener=[EndCall("Goodbye.", "agent_hangup")])
    async with harness(config=FAST, brain=brain) as h:
        result = await h.result()
    assert (result.end_reason, h.brain.no_input_calls) == ("agent_hangup", 0)


async def test_a_reprompt_can_be_interrupted() -> None:
    brain = ScriptedBrain(
        opener=[SpeakStatic("Hi.")],
        no_input=[
            [SpeakText(hanging("Are you still there? I can wait while you look it up, no rush at all. "))]
        ],
        replies=[[SpeakStatic("Great.")]],
    )
    async with harness(config=FAST, brain=brain, utterances=[Said("yes I am still here")]) as h:
        await h.until(lambda: h.brain.no_input_calls == 1 and h.pipeline.speaker.tracker is not None)
        await h.until(lambda: h.pipeline.speaker.is_playing(h.now_ms()))
        await h.caller.say(900)
        await h.until(lambda: len(h.observer.interrupts) == 1)
        await h.until(lambda: len(h.brain.user_turns) == 1)
        assert h.brain.user_turns[0].text == "yes I am still here"
        assert h.pipeline.no_input.reprompts == 0


async def test_expiry_logs_reprompt_and_timeout() -> None:
    stream = json_log_stream()
    config = PipelineConfig(no_input_timeout_ms=TIMEOUT_MS, max_reprompts=1)
    async with harness(config=config, brain=hi()) as h:
        await h.result()
    events = [e for e in log_events(stream) if e["event"].startswith("pipeline_no_input")]
    assert [(e["event"], e["reprompts"]) for e in events] == [
        ("pipeline_no_input_reprompt", 1),
        ("pipeline_no_input_timeout", 1),
    ]


@pytest.mark.parametrize("timeout_ms", [3000, 30_000])
async def test_the_configured_timeout_is_used(timeout_ms: int) -> None:
    config = PipelineConfig(no_input_timeout_ms=timeout_ms, max_duration_s=120.0)
    async with harness(config=config, brain=hi()) as h:
        await h.until(lambda: h.brain.no_input_calls == 1, timeout_s=60.0)
        elapsed = h.now_ms() - h.observer.agent_turns[0].ended_ms
    assert timeout_ms <= elapsed <= timeout_ms + WINDOW_MS
