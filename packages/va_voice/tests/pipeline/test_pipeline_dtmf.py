"""CallPipeline DTMF input: sequences, terminator, ignore_button_press, barge-in (specs/07 §13, P04-T12c)."""

from __future__ import annotations

import pytest
from pipeline_fakes import Said, ScriptedBrain
from pipeline_harness import Harness, harness
from service_helpers import json_log_stream, log_events

from va_voice.pipeline import (
    DTMF_SEQUENCE_TIMEOUT_MS,
    InboundDtmf,
    PipelineConfig,
    SpeakStatic,
)

WORDS = (
    "Thanks for calling the clinic today. I can help you book, move or cancel an appointment, and I can also "
    "answer questions about our opening hours, parking, insurance and the documents you should bring along. "
)
LONG = (WORDS * 3).strip()
"""About 600 characters: 3 s of fake TTS audio."""
STEP_MS = 20.0


async def press(h: Harness, digits: str, gap_s: float = 0.1) -> list[float]:
    """Press ``digits`` ``gap_s`` apart; returns the virtual ms of each press."""
    pressed: list[float] = []
    for i, digit in enumerate(digits):
        if i:
            await h.clock.sleep(gap_s)
        pressed.append(h.now_ms())
        h.caller.press(digit)
    return pressed


async def first_turn(h: Harness) -> None:
    await h.until(lambda: len(h.brain.user_turns) == 1)


async def playing(h: Harness) -> None:
    await h.until(lambda: h.pipeline.speaker.is_playing(h.now_ms()))


async def test_a_sequence_is_delivered_after_the_timeout() -> None:
    async with harness() as h:
        pressed = await press(h, "1234")
        await first_turn(h)
        delivered = h.now_ms()
        turn = h.brain.user_turns[0]
        assert (turn.text, turn.dtmf, turn.words) == ("[DTMF: 1 2 3 4]", "1234", ())
        assert turn.start_ms == pytest.approx(pressed[0])
        assert turn.end_ms == pytest.approx(pressed[-1])
        assert 0 <= delivered - (pressed[-1] + DTMF_SEQUENCE_TIMEOUT_MS) <= STEP_MS
        assert h.observer.user_turns == [turn]
        assert h.observer.reports == []
        assert h.pipeline.stats.dtmf_turns == 1


async def test_hash_ends_the_sequence_at_once() -> None:
    async with harness() as h:
        pressed = await press(h, "#98#")
        await first_turn(h)
        assert h.now_ms() - pressed[-1] <= STEP_MS
        assert h.brain.user_turns[0].dtmf == "98"
        assert h.brain.user_turns[0].text == "[DTMF: 9 8]"
        await press(h, "*5#")
        await h.until(lambda: len(h.brain.user_turns) == 2)
        assert h.brain.user_turns[1].dtmf == "*5"
        assert h.pipeline.stats.dtmf_turns == 2


async def test_ignore_button_press_drops_every_digit() -> None:
    config = PipelineConfig(ignore_button_press=True)
    brain = ScriptedBrain(opener=[SpeakStatic(LONG)])
    async with harness(config=config, brain=brain) as h:
        await playing(h)
        await press(h, "12#")
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        await h.until(lambda: h.now_ms() >= 6000)
        assert h.brain.user_turns == []
        assert h.transport.clears == []
        assert not h.observer.agent_turns[0].interrupted
        assert (h.pipeline.stats.ignored_dtmf, h.pipeline.stats.dtmf_turns) == (3, 0)


async def test_a_digit_interrupts_playback_at_level_1() -> None:
    config = PipelineConfig(interruptibility=1)
    brain = ScriptedBrain(opener=[SpeakStatic(LONG)], replies=[[SpeakStatic("Booking it is.")]])
    async with harness(config=config, brain=brain) as h:
        await playing(h)
        await h.clock.sleep(0.5)
        (pressed,) = await press(h, "1")
        await h.until(lambda: len(h.observer.interrupts) == 1)
        record = h.observer.interrupts[0]
        assert (record.reason, record.level, record.speech_start_ms) == ("dtmf", 1, pressed)
        assert record.reaction_ms <= 120
        assert h.transport.clears[0] - pressed <= 120
        assert h.observer.agent_turns[0].interrupted
        await first_turn(h)
        assert h.brain.user_turns[0].dtmf == "1"
        await h.until(lambda: len(h.observer.agent_turns) == 2)
        # A digit is real input: no false-barge-in resume.
        assert h.observer.agent_turns[1].text == "Booking it is."
        assert h.pipeline.stats.false_barge_ins == 0


@pytest.mark.parametrize(
    ("config", "interruptible"),
    [(PipelineConfig(interruptibility=0), True), (PipelineConfig(interruptibility=4), False)],
    ids=["level0", "non_interruptible_line"],
)
async def test_a_digit_does_not_interrupt_blocked_playback(
    config: PipelineConfig, interruptible: bool
) -> None:
    brain = ScriptedBrain(opener=[SpeakStatic(LONG, interruptible=interruptible)])
    async with harness(config=config, brain=brain) as h:
        await playing(h)
        await press(h, "7#")
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        opener = h.observer.agent_turns[0]
        assert (opener.interrupted, h.transport.clears, h.observer.interrupts) == (False, [], [])
        # Held behind the audible opener and delivered once it finished.
        await first_turn(h)
        assert h.brain.user_turns[0].dtmf == "7"
        assert h.pipeline.stats.held_turns == 1
        assert h.brain.user_turns[0].start_ms < opener.ended_ms


async def test_invalid_digits_are_dropped_and_logged_without_their_value() -> None:
    stream = json_log_stream()
    async with harness() as h:
        h.transport.push(InboundDtmf("x"))
        await press(h, "8675309#", gap_s=0.05)
        await first_turn(h)
        assert h.brain.user_turns[0].dtmf == "8675309"
    events = log_events(stream)
    invalid = [e for e in events if e["event"] == "pipeline_dtmf_invalid"]
    assert invalid == [{**invalid[0], "length": 1}]
    turn_logs = [e for e in events if e["event"] == "pipeline_dtmf_turn"]
    assert [e["digits"] for e in turn_logs] == [7]
    # No timestamp holds a run of 7 digits, so the sequence would only appear if it were logged.
    assert "8675309" not in stream.getvalue()


async def test_digits_disarm_the_no_input_timer_and_reset_the_count() -> None:
    config = PipelineConfig(no_input_timeout_ms=3000, max_reprompts=1)
    brain = ScriptedBrain(opener=[SpeakStatic("Hi.")], replies=[[SpeakStatic("Thanks.")]])
    async with harness(config=config, brain=brain) as h:
        await h.until(lambda: h.brain.no_input_calls == 1)
        await h.until(lambda: len(h.observer.agent_turns) == 2)
        reprompt_end = h.observer.agent_turns[1].ended_ms
        await h.until(lambda: h.now_ms() >= reprompt_end + 2500)
        await press(h, "5")
        # The digit stopped the timer: no goodbye at reprompt_end + 3000.
        await first_turn(h)
        assert h.now_ms() > reprompt_end + 4000
        assert h.pipeline.no_input.reprompts == 0
        assert [t.text for t in h.observer.agent_turns][:2] == ["Hi.", "Are you still there?"]
        await h.until(lambda: h.brain.no_input_calls == 2)


async def test_pending_digits_are_dropped_when_the_transport_closes() -> None:
    stream = json_log_stream()
    async with harness() as h:
        await press(h, "12")
        h.transport.close()
        result = await h.result()
    assert (result.end_reason, h.brain.user_turns, result.stats.dtmf_turns) == ("transport_closed", [], 0)
    dropped = [e for e in log_events(stream) if e["event"] == "pipeline_dtmf_dropped"]
    assert [e["digits"] for e in dropped] == [2]


async def test_digits_merge_with_a_held_speech_turn() -> None:
    config = PipelineConfig(interruptibility=0)
    brain = ScriptedBrain(opener=[SpeakStatic(LONG)])
    async with harness(config=config, brain=brain, utterances=[Said("my account number is")]) as h:
        await playing(h)
        await h.caller.say(600)
        await h.until(lambda: h.pipeline.stats.held_turns == 1)
        await press(h, "889#")
        await first_turn(h)
        turn = h.brain.user_turns[0]
        assert (turn.text, turn.dtmf) == ("my account number is [DTMF: 8 8 9]", "889")
        assert h.pipeline.stats.held_turns == 2
