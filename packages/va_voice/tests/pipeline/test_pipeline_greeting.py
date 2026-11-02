"""CallPipeline call opening: ``before_opener`` and ``wait_for_greeting`` (specs/07 §11, P05-T10b)."""

from __future__ import annotations

from pipeline_fakes import ScriptedBrain
from pipeline_harness import Harness, harness

from va_sim.clock import AcceleratedClock
from va_voice.pipeline import DEFAULT_NO_INPUT_PROMPT, PipelineConfig, SpeakStatic

OPENER = "Hi, this is Ava."
WINDOW_MS = 40.0
"""A 20 ms harness step plus a few event-loop turns."""


def opener() -> ScriptedBrain:
    return ScriptedBrain(opener=[SpeakStatic(OPENER)])


def times(h: Harness, name: str) -> list[float]:
    return [t for n, t in h.transport.calls if n == name]


def greeting_config(**overrides: object) -> PipelineConfig:
    values: dict[str, object] = {"wait_for_greeting": True, "no_input_timeout_ms": 30_000}
    values.update(overrides)
    return PipelineConfig(**values)  # type: ignore[arg-type]


async def test_the_hook_runs_before_the_opener_and_delays_it() -> None:
    clock = AcceleratedClock()
    events: list[tuple[str, float]] = []

    async def gate() -> None:
        events.append(("hook.start", clock.monotonic() * 1000))
        await clock.sleep(1.5)
        events.append(("hook.end", clock.monotonic() * 1000))

    async with harness(brain=opener(), clock=clock, before_opener=gate) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 1)
    assert [name for name, _ in events] == ["hook.start", "hook.end"]
    hook_end = events[1][1]
    assert hook_end >= events[0][1] + 1500
    (started,) = times(h, "brain.start")
    assert hook_end <= started <= hook_end + WINDOW_MS
    assert times(h, "tts.start_context")[0] >= hook_end
    assert h.brain.greetings == [None]


async def test_without_wait_for_greeting_the_opener_starts_at_once() -> None:
    async with harness(brain=opener()) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        stats = h.pipeline.stats
    assert times(h, "brain.start")[0] <= WINDOW_MS
    assert h.brain.greetings == [None]
    assert (stats.greeting_turns, stats.greeting_timeouts) == (0, 0)


async def test_wait_for_greeting_holds_the_opener_until_the_callee_speaks() -> None:
    async with harness(config=greeting_config(), brain=opener(), utterances=["Hello?"]) as h:
        await h.until(lambda: h.now_ms() >= 1000)
        assert h.transport.calls == []
        await h.caller.say(600)
        await h.until(lambda: len(h.brain.greetings) == 1)
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        stats = h.pipeline.stats
    (greeting,) = h.brain.greetings
    assert greeting is not None
    assert greeting.text == "Hello?"
    assert greeting.dtmf is None
    # The callee's turn opened the call: it is the opener's input, never a separate user turn.
    assert h.brain.user_turns == []
    assert [turn.text for turn in h.observer.user_turns] == ["Hello?"]
    assert h.observer.agent_turns[0].text == OPENER
    assert h.observer.agent_turns[0].started_ms > greeting.end_ms
    assert (stats.greeting_turns, stats.greeting_timeouts) == (1, 0)


async def test_a_keypad_sequence_is_a_greeting_too() -> None:
    async with harness(config=greeting_config(), brain=opener()) as h:
        await h.until(lambda: h.now_ms() >= 500)
        h.caller.press("4")
        h.caller.press("2")
        h.caller.press("#")
        await h.until(lambda: len(h.observer.agent_turns) == 1)
    (greeting,) = h.brain.greetings
    assert greeting is not None
    assert greeting.dtmf == "42"
    assert times(h, "brain.start")[0] < 500 + WINDOW_MS


async def test_silence_starts_the_opener_after_the_greeting_timeout() -> None:
    config = greeting_config(greeting_timeout_ms=2000)
    async with harness(config=config, brain=opener()) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        stats = h.pipeline.stats
    assert h.brain.greetings == [None]
    (started,) = times(h, "brain.start")
    assert 2000 <= started <= 2000 + WINDOW_MS
    assert (stats.greeting_turns, stats.greeting_timeouts) == (0, 1)


async def test_no_reprompt_fires_while_waiting_for_the_greeting() -> None:
    config = greeting_config(greeting_timeout_ms=10_000, no_input_timeout_ms=3000)
    async with harness(config=config, brain=opener()) as h:
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        assert h.brain.no_input_calls == 0
        await h.until(lambda: len(h.observer.agent_turns) == 2)
    first, reprompt = h.observer.agent_turns
    assert 10_000 <= first.started_ms <= 10_000 + WINDOW_MS
    assert reprompt.text == DEFAULT_NO_INPUT_PROMPT
    assert 3000 <= reprompt.started_ms - first.ended_ms <= 3000 + WINDOW_MS
    assert h.brain.no_input_calls == 1


async def test_turns_during_the_hook_without_greeting_wait_are_answered_after_the_opener() -> None:
    clock = AcceleratedClock()

    async def gate() -> None:
        await clock.sleep(3.0)

    brain = ScriptedBrain(opener=[SpeakStatic(OPENER)], replies=[[SpeakStatic("Sure.")]])
    config = PipelineConfig(no_input_timeout_ms=30_000)
    async with harness(
        config=config, brain=brain, utterances=["Hi there"], clock=clock, before_opener=gate
    ) as h:
        await h.caller.say(600)
        await h.until(lambda: len(h.observer.agent_turns) == 2)
    assert [turn.text for turn in h.observer.agent_turns] == [OPENER, "Sure."]
    assert h.brain.greetings == [None]
    assert [turn.text for turn in h.brain.user_turns] == ["Hi there"]
