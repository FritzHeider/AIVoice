"""``barge_in`` (specs/30 §5): the caller talks over a 300-character opener; the agent clears its audio
within the reaction budget, the assistant turn is marked interrupted, and the next LLM request carries only
the spoken part of the opener (Decision P05-T17d under specs/30 §5: interruptibility 4, so the decision does
not wait for STT words)."""

from __future__ import annotations

from typing import Any, Final

import pytest
from e2e_harness import Harness
from e2e_texts import LONG_FIRST_SENTENCE, LONG_SENTENCE_CHARS, TASK
from e2e_timing import assistant_before, first_clear_after, interrupt_events, whole_word_prefix

from va_llm.prompts import INTERRUPTION_MARK
from va_sim.audio import MARKER_MS
from va_sim.elevenlabs.tts_audio import CHAR_MS
from va_voice.interrupt import ALWAYS, LEVEL_RULES
from va_voice.vad import START_WINDOWS, WINDOW_MS

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]

CALLER_TEXT: Final = "Wait, I have a quick question about my order."
BARGE_IN_AFTER_MS: Final = 1200
REACTION_BUDGET_MS: Final = 120
FRAME_MS: Final = 20
# Level 4 interrupts after this much caller speech (specs/07 §7).
MIN_SPEECH_MS: Final = LEVEL_RULES[ALWAYS].min_speech_ms
# The synthetic caller voice opens with one 180 ms raised-cosine syllable rising from -12 dB
# (`va_sim.audio.synth`); Silero may only call its speech from the second syllable.
FIRST_SYLLABLE_MS: Final = 180
# Caller voice onset → `clear` at FakeTelnyx: the level's speech minimum, Silero's onset (first syllable plus
# the start confirmation), the 120 ms reaction budget, and one 20 ms frame each way over the media socket.
ONSET_TO_CLEAR_MS: Final = (
    MIN_SPEECH_MS + FIRST_SYLLABLE_MS + START_WINDOWS * WINDOW_MS + REACTION_BUDGET_MS + 2 * FRAME_MS
)
# Pipeline speech start → clear: the speech minimum, one VAD window of granularity, and the reaction budget.
REACTION_MS: Final = MIN_SPEECH_MS + WINDOW_MS + REACTION_BUDGET_MS
CHARS_TOLERANCE: Final = 0.10


def check_spoken(messages: list[Any], played_chars: int) -> None:
    """The first LLM request after the barge-in holds the opener cut at what was spoken, then the mark."""
    before = assistant_before(messages, CALLER_TEXT)
    assert before is not None, messages
    assert before.endswith(INTERRUPTION_MARK), before
    spoken = before.removesuffix(INTERRUPTION_MARK)
    assert spoken, before
    assert LONG_FIRST_SENTENCE.startswith(spoken), spoken
    assert len(spoken) <= played_chars, (spoken, played_chars)
    assert len(spoken) < LONG_SENTENCE_CHARS, spoken
    remainder = LONG_FIRST_SENTENCE[len(spoken) + 40 :]
    contents = [str(message.get("content")) for message in messages]
    assert not any(remainder in content for content in contents), remainder


async def test_barge_in(harness: Harness) -> None:
    assert len(LONG_FIRST_SENTENCE) == LONG_SENTENCE_CHARS
    org = await harness.create_org()
    scenario = await harness.load_scenario("barge_in")
    call_id = await harness.send_task_call(
        org, scenario, TASK, first_sentence=LONG_FIRST_SENTENCE, interruptibility=ALWAYS
    )
    finished = await harness.finished(org, call_id, number=scenario.number)
    call, timeline = finished.call, finished.timeline
    assert not finished.failures(scenario), (call, finished.events, timeline)

    utterance = timeline["utterances"][0]
    assert utterance["text"] == CALLER_TEXT, timeline
    onset = utterance["start_ms"] + MARKER_MS
    clear = first_clear_after(timeline, utterance["start_ms"])
    assert clear is not None, timeline
    [interrupt] = interrupt_events(finished.events)
    data = interrupt["data"]
    print(f"barge_in: onset->clear {clear - onset} ms, interrupt {data}")  # noqa: T201
    assert clear - onset <= ONSET_TO_CLEAR_MS, (onset, clear, timeline["clears"])
    assert data["reaction_ms"] <= REACTION_MS, data
    assert data["played_ms"] >= BARGE_IN_AFTER_MS, data
    # `played_chars` counts whole words heard: within ±10 % of the audio position, cut back to a word end.
    expected_chars = data["played_ms"] / CHAR_MS
    lowest = whole_word_prefix(LONG_FIRST_SENTENCE, (1 - CHARS_TOLERANCE) * expected_chars)
    assert lowest <= data["played_chars"] <= (1 + CHARS_TOLERANCE) * expected_chars, (data, lowest)
    assert data["total_chars"] == LONG_SENTENCE_CHARS, data

    assistant = [turn for turn in call["transcripts"] if turn["user"] == "assistant"]
    assert assistant[0]["text"] == LONG_FIRST_SENTENCE, assistant
    assert assistant[0]["interrupted"] is True, assistant
    requests = [
        request
        for request in await harness.llm_requests(call_id)
        if any(CALLER_TEXT in str(message.get("content")) for message in request["messages"])
    ]
    assert requests, await harness.llm_requests(call_id)
    check_spoken(requests[0]["messages"], data["played_chars"])
