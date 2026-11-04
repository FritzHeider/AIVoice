"""``interruptibility_levels`` (specs/30 §5): per call ``interruptibility`` 0-4, a backchannel ("uh-huh")
and then a full sentence are spoken over the agent; only the levels' rules decide which of them clears the
agent's audio (specs/07 §7 table, ``va_voice.interrupt.LEVEL_RULES``; Decision P05-T17d under specs/30 §5)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

import pytest
from e2e_harness import FinishedCall, Harness
from e2e_texts import LONG_FIRST_SENTENCE, TASK
from e2e_timing import (
    ONSET_TOLERANCE_MS,
    Window,
    clears_in,
    decision_speech_ms,
    interrupt_events,
    utterance_window,
)

from va_sim.audio import MARKER_MS
from va_voice.interrupt import ALWAYS, BLOCK, LEVEL_RULES, InterruptLevel

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]

BACKCHANNEL: Final = "uh-huh"
SENTENCE: Final = "Sorry, could you tell me your opening hours on the weekend?"
# A clear that answers an utterance arrives at most this long after its last caller frame.
TAIL_MS: Final = 500
LEVELS: Final[tuple[InterruptLevel, ...]] = (0, 1, 2, 3, 4)


def windows(finished: FinishedCall) -> tuple[Window, Window, dict[str, Any], dict[str, Any]]:
    utterances = finished.timeline["utterances"]
    texts = [entry["text"] for entry in utterances]
    assert texts[:2] == [BACKCHANNEL, SENTENCE], utterances
    backchannel, sentence = utterances[0], utterances[1]
    return utterance_window(backchannel, TAIL_MS), utterance_window(sentence, TAIL_MS), backchannel, sentence


def check_interrupted_after(
    clears: list[int], utterance: dict[str, Any], level: InterruptLevel, interrupt: Mapping[str, Any]
) -> None:
    """Exactly one clear for the utterance, decided on at least the level's speech minimum.

    The policy's own measure (``call.interrupt`` ``speech_ms``) must reach the minimum; the sim clear is
    checked against voice onset with :data:`ONSET_TOLERANCE_MS`, because Silero's ``speech_start`` is the
    start of a 32 ms window that may begin before the onset (Decision P08-T00 under specs/30 §5).
    """
    minimum = LEVEL_RULES[level].min_speech_ms
    assert len(clears) == 1, (level, clears, utterance)
    assert interrupt["data"]["level"] == level, interrupt
    assert decision_speech_ms(interrupt) >= minimum, (level, interrupt)
    onset = utterance["start_ms"] + MARKER_MS
    assert clears[0] - onset >= minimum - ONSET_TOLERANCE_MS, (level, clears, utterance, interrupt)


@pytest.mark.parametrize("level", LEVELS)
async def test_interruptibility_levels(harness: Harness, level: InterruptLevel) -> None:
    org = await harness.create_org()
    scenario = await harness.load_scenario("interruptibility_levels")
    call_id = await harness.send_task_call(
        org, scenario, TASK, first_sentence=LONG_FIRST_SENTENCE, interruptibility=level
    )
    finished = await harness.finished(org, call_id, number=scenario.number)
    assert not finished.failures(scenario), (finished.call, finished.events, finished.timeline)

    timeline = finished.timeline
    backchannel_window, sentence_window, backchannel, sentence = windows(finished)
    backchannel_clears = clears_in(timeline, backchannel_window)
    sentence_clears = clears_in(timeline, sentence_window)
    interrupts = interrupt_events(finished.events)
    print(  # noqa: T201
        f"interruptibility {level}: uh-huh {backchannel} clears {backchannel_clears}; "
        f"sentence {sentence} clears {sentence_clears}; interrupts {[e['data'] for e in interrupts]}"
    )
    interrupted_turns = [turn for turn in finished.call["transcripts"] if turn["interrupted"]]
    if level == BLOCK:
        assert (backchannel_clears, sentence_clears) == ([], []), timeline
        assert interrupts == [], interrupts
        assert interrupted_turns == [], interrupted_turns
        return
    if level == ALWAYS:
        assert len(interrupts) == 2, interrupts
        check_interrupted_after(backchannel_clears, backchannel, level, interrupts[0])
    else:
        assert backchannel_clears == [], (level, timeline)
        assert len(interrupts) == 1, interrupts
    check_interrupted_after(sentence_clears, sentence, level, interrupts[-1])
    assert len(interrupted_turns) == len(interrupts), interrupted_turns
