"""Playback tracker and barge-in truncation (specs/07-media-pipeline.md §7 step 6, §9; Decision P04-T08)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from text_helpers import Alignment

from va_voice.audio.formats import FRAME_MS, ULAW_8000
from va_voice.playback import (
    INTERRUPTED_SUFFIX,
    LEAD_MS_ESTIMATE,
    MARK_LOSS_GRACE_MS,
    PlaybackTracker,
    Segment,
    mark_name,
    parse_mark,
    truncate_assistant_message,
)
from va_voice.text.alignment import AlignmentTimeline
from va_voice.text.pronunciation import PronunciationApplier, PronunciationRule


def send_frames(tracker: PlaybackTracker, count: int, *, start_ms: float = 0.0) -> None:
    for index in range(count):
        tracker.frame_sent(start_ms + index * FRAME_MS)


def streamed(frames: int = 10, segments: int = 1) -> PlaybackTracker:
    tracker = PlaybackTracker(3)
    per_segment = frames // segments
    for _ in range(segments):
        send_frames(tracker, per_segment, start_ms=tracker.frames_sent * FRAME_MS)
        tracker.end_segment(5)
    return tracker


def state(tracker: PlaybackTracker) -> tuple[object, ...]:
    return (tracker.finished, tracker.stopped, tracker.frames_sent, tracker.total_ms, tracker.confirmed_ms)


def test_mark_names_round_trip() -> None:
    assert mark_name(4, 2) == "seg:4:2"
    assert parse_mark("seg:4:2") == (4, 2)
    assert parse_mark(mark_name(12, 0)) == (12, 0)


@pytest.mark.parametrize("name", ["seg:1", "seg:a:1", "x:1:2", "seg:1:2:3", "seg:-1:2", " seg:1:2", ""])
def test_foreign_mark_names_do_not_parse(name: str) -> None:
    assert parse_mark(name) is None


def test_negative_turn_is_rejected() -> None:
    with pytest.raises(ValueError, match="turn"):
        PlaybackTracker(-1)


def test_played_ms_before_the_first_frame_is_zero() -> None:
    tracker = PlaybackTracker(1)
    assert tracker.played_ms(1_000) == 0
    assert tracker.t_first_frame_sent is None
    assert not tracker.is_playing(1_000)


@pytest.mark.parametrize(
    ("now_ms", "expected"),
    [(1_000, 0), (1_000 + LEAD_MS_ESTIMATE, 0), (1_100, 40), (1_260, 200), (5_000, 200)],
)
def test_played_ms_subtracts_the_lead_and_clamps_to_frames_sent(now_ms: float, expected: float) -> None:
    tracker = PlaybackTracker(1)
    send_frames(tracker, 10, start_ms=1_000)
    assert tracker.t_first_frame_sent == 1_000
    assert tracker.total_ms == 200
    assert tracker.played_ms(now_ms) == expected


def test_custom_lead() -> None:
    tracker = PlaybackTracker(1, lead_ms=0)
    send_frames(tracker, 10)
    assert tracker.played_ms(50) == 50


def test_mark_echo_raises_the_floor() -> None:
    tracker = PlaybackTracker(7)
    send_frames(tracker, 5)
    first = tracker.end_segment(4)
    send_frames(tracker, 5, start_ms=100)
    second = tracker.end_segment(6)
    assert (first, second, tracker.turn) == ("seg:7:0", "seg:7:1", 7)
    assert tracker.segments == (Segment(0, 0, 100, 4), Segment(1, 100, 200, 6))
    assert tracker.played_ms(70) == 10
    assert tracker.on_mark(first)
    assert tracker.played_ms(70) == 100
    assert tracker.on_mark(second)
    assert tracker.played_ms(70) == 200
    assert tracker.on_mark(first)
    assert tracker.confirmed_ms == 200


@pytest.mark.parametrize("name", ["seg:8:0", "seg:7:5", "garbage"])
def test_marks_of_other_utterances_are_ignored(name: str) -> None:
    tracker = PlaybackTracker(7)
    send_frames(tracker, 5)
    tracker.end_segment(4)
    assert not tracker.on_mark(name)
    assert tracker.confirmed_ms == 0


def test_is_playing_stays_true_while_audio_may_still_come() -> None:
    tracker = PlaybackTracker(1)
    send_frames(tracker, 5)
    tracker.end_segment(1)
    assert tracker.is_playing(0)
    assert tracker.is_playing(100_000)
    assert not tracker.completed(100_000)


def test_is_playing_ends_on_the_last_mark_echo() -> None:
    tracker = streamed(frames=10, segments=2)
    tracker.on_mark("seg:3:1")
    assert tracker.is_playing(50)
    tracker.finish()
    assert not tracker.is_playing(50)
    assert tracker.completed(50)


def test_is_playing_ends_after_the_grace_when_marks_are_lost() -> None:
    tracker = streamed(frames=10, segments=2)
    tracker.finish()
    tracker.on_mark("seg:3:0")
    end = tracker.total_ms + LEAD_MS_ESTIMATE + MARK_LOSS_GRACE_MS
    assert tracker.is_playing(end - 1)
    assert not tracker.is_playing(end)
    assert tracker.completed(end)


def test_stop_freezes_playback_and_is_idempotent() -> None:
    tracker = streamed(frames=10)
    assert tracker.stop(160) == 100
    assert tracker.stopped
    assert tracker.played_ms(10_000) == 100
    assert tracker.played_ms(100) == 40
    assert tracker.stop(190) == 100
    assert not tracker.is_playing(170)
    assert not tracker.on_mark("seg:3:0")
    tracker.frame_sent(200)
    assert tracker.frames_sent == 10
    tracker.finish()
    assert not tracker.completed(10_000)


def test_invalid_sequences_raise() -> None:
    tracker = PlaybackTracker(1)
    with pytest.raises(ValueError, match="at least one frame"):
        tracker.end_segment(0)
    send_frames(tracker, 2)
    with pytest.raises(RuntimeError, match="end_segment before finish"):
        tracker.finish()
    tracker.end_segment(2)
    tracker.finish()
    assert state(tracker) == (True, False, 2, 40, 0)
    with pytest.raises(RuntimeError, match="finished"):
        tracker.frame_sent(100)
    with pytest.raises(RuntimeError, match="finished or stopped"):
        tracker.end_segment(1)


def test_finish_without_audio_never_plays() -> None:
    tracker = PlaybackTracker(1)
    tracker.finish()
    assert not tracker.is_playing(0)
    assert tracker.completed(0)


def _utterance(*clauses: str, ms_per_char: int = 10) -> PlaybackTracker:
    """A tracker whose timeline holds ``clauses`` at ``ms_per_char`` (one chunk per clause)."""
    tracker = PlaybackTracker(2)
    timeline = tracker.timeline
    for clause in clauses:
        timeline.add_clause(clause)
        starts = tuple(i * ms_per_char for i in range(len(clause)))
        audio = b"\xff" * (8 * ms_per_char * len(clause))
        timeline.add_chunk(audio, Alignment(tuple(clause), starts, (ms_per_char,) * len(clause)))
    frames = -(-int(timeline.offset_ms) // FRAME_MS)
    send_frames(tracker, frames)
    tracker.end_segment(timeline.total_chars)
    tracker.finish()
    return tracker


def test_played_text_uses_the_tracker_estimate() -> None:
    tracker = _utterance("Hello there friend")
    assert tracker.timeline.format is ULAW_8000
    assert tracker.played_text(LEAD_MS_ESTIMATE + 110).text == "Hello there"
    assert tracker.played_text(LEAD_MS_ESTIMATE + 109).text == "Hello"


@pytest.mark.parametrize(
    ("interrupt_at", "spoken", "unspoken"),
    [
        (0, "", "Thanks for calling. How can I help?"),
        (60 + 59, "", "Thanks for calling. How can I help?"),
        (60 + 60, "Thanks", "for calling. How can I help?"),
        (60 + 195, "Thanks for calling.", "How can I help?"),
        (60 + 345, "Thanks for calling. How can I", "help?"),
    ],
)
def test_truncation_at_interrupt_times(interrupt_at: float, spoken: str, unspoken: str) -> None:
    tracker = _utterance("Thanks for calling. ", "How can I help? ")
    message = truncate_assistant_message(tracker.timeline, tracker.stop(interrupt_at))
    assert message.text == "Thanks for calling. How can I help?"
    assert message.text_spoken == spoken
    assert message.history_text == f"{spoken}{INTERRUPTED_SUFFIX}"
    assert message.unspoken == unspoken
    assert (message.played_chars, message.total_chars) == (len(spoken), len(message.text))


def test_full_playback_confirmed_by_mark_is_the_whole_text() -> None:
    tracker = _utterance("Thanks for calling. ", "How can I help? ")
    tracker.on_mark("seg:2:0")
    message = truncate_assistant_message(tracker.timeline, tracker.played_ms(0))
    assert message.text_spoken == message.text
    assert message.unspoken == ""


def test_truncation_keeps_original_words_of_replaced_text() -> None:
    applier = PronunciationApplier([PronunciationRule("SQL", "sequel")])
    timeline = AlignmentTimeline(ULAW_8000)
    clause = applier.apply("  Learn SQL today ")
    timeline.add_clause(clause)
    timeline.add_chunk(
        b"\xff" * 8 * 10 * len(clause.text),
        Alignment(
            tuple(clause.text), tuple(i * 10 for i in range(len(clause.text))), (10,) * len(clause.text)
        ),
    )
    message = truncate_assistant_message(timeline, 120)
    assert message.text == "Learn SQL today"
    assert message.text_spoken == "Learn"
    assert message.unspoken == "SQL today"
    assert truncate_assistant_message(timeline, 139).text_spoken == "Learn"
    assert truncate_assistant_message(timeline, 140).text_spoken == "Learn SQL"


_WORD = st.text(alphabet="abcdefXY.,?", min_size=1, max_size=7)


@settings(max_examples=150, deadline=None)
@given(
    words=st.lists(_WORD, min_size=1, max_size=15),
    ms_per_char=st.integers(1, 30),
    interrupt_at=st.floats(0, 3_000, allow_nan=False),
)
def test_truncation_always_cuts_on_a_word_boundary(
    words: list[str], ms_per_char: int, interrupt_at: float
) -> None:
    text = " ".join(words) + " "
    middle = len(text) // 2
    tracker = _utterance(text[:middle], text[middle:], ms_per_char=ms_per_char)
    message = truncate_assistant_message(tracker.timeline, tracker.stop(interrupt_at))
    assert message.played_chars <= message.total_chars
    assert message.text.startswith(message.text_spoken)
    assert message.text_spoken == " ".join(words[: len(message.text_spoken.split())])
    assert message.history_text.endswith(INTERRUPTED_SUFFIX)
    assert " ".join(part for part in (message.text_spoken, message.unspoken) if part) == message.text
