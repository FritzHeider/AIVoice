"""Alignment timeline (specs/09-tts-elevenlabs.md §7 and its Decision P04-T08)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from text_helpers import Alignment

from va_voice.audio.formats import PCM16LE_16000, ULAW_8000
from va_voice.text.alignment import AlignmentTimeline, CharTiming, SpokenPrefix
from va_voice.text.pronunciation import PronunciationApplier, PronunciationRule


def aligned(text: str, ms_per_char: int = 10, *, lead_ms: int = 0) -> tuple[bytes, Alignment]:
    """μ-law audio covering ``text`` at ``ms_per_char`` and its chunk-relative alignment."""
    starts = tuple(lead_ms + i * ms_per_char for i in range(len(text)))
    durations = (ms_per_char,) * len(text)
    audio = b"\xff" * (8 * (lead_ms + ms_per_char * len(text)))
    return audio, Alignment(tuple(text), starts, durations)


def timeline_of(*texts: str, ms_per_char: int = 10) -> AlignmentTimeline:
    timeline = AlignmentTimeline(ULAW_8000)
    for text in texts:
        timeline.add_clause(text)
        timeline.add_chunk(*aligned(text, ms_per_char))
    return timeline


def spoken(timeline: AlignmentTimeline, played_ms: float) -> str:
    return timeline.text_until(played_ms).text


@pytest.mark.parametrize(
    ("played_ms", "expected"),
    [
        (0, ""),
        (49, ""),
        (50, "Hello"),
        (60, "Hello"),
        (110, "Hello there"),
        (150, "Hello there"),
        (179, "Hello there"),
        (180, "Hello there friend"),
        (10_000, "Hello there friend"),
        (-5, ""),
    ],
)
def test_text_until_returns_whole_words_only(played_ms: float, expected: str) -> None:
    timeline = timeline_of("Hello there friend")
    assert timeline.text_until(played_ms) == SpokenPrefix(expected, len(expected))


def test_chunk_relative_times_are_made_absolute() -> None:
    timeline = AlignmentTimeline(ULAW_8000)
    timeline.add_clause("one two ")
    first_audio, first = aligned("one ", 10)
    second_audio, second = aligned("two ", 10)
    timeline.add_chunk(first_audio, first)
    timeline.add_chunk(second_audio, second)
    assert timeline.offset_ms == 80
    assert timeline.char_end_ms(4) == 50
    assert timeline.char_end_ms(6) == 70
    assert spoken(timeline, 69) == "one"
    assert spoken(timeline, 70) == "one two"


def test_punctuation_belongs_to_its_word() -> None:
    timeline = timeline_of("Hi, Bob.")
    assert spoken(timeline, 20) == ""
    assert spoken(timeline, 30) == "Hi,"
    assert spoken(timeline, 70) == "Hi,"
    assert spoken(timeline, 80) == "Hi, Bob."


def test_clauses_concatenate_on_one_time_axis() -> None:
    timeline = timeline_of("Thanks for calling. ", "How can I help? ")
    assert timeline.original_text == "Thanks for calling. How can I help? "
    assert timeline.total_chars == len(timeline.original_text)
    assert spoken(timeline, 190) == "Thanks for calling."
    assert spoken(timeline, 230) == "Thanks for calling. How"
    assert spoken(timeline, 360) == "Thanks for calling. How can I help?"


def test_pronunciation_replacements_map_back_to_the_original_words() -> None:
    applier = PronunciationApplier([PronunciationRule("SQL", "sequel")])
    clause = applier.apply("Call SQL now ")
    timeline = AlignmentTimeline(ULAW_8000)
    timeline.add_clause(clause)
    timeline.add_chunk(*aligned(clause.text, 10))
    assert timeline.sent_text == "Call sequel now "
    assert timeline.original_text == "Call SQL now "
    assert spoken(timeline, 40) == "Call"
    assert spoken(timeline, 100) == "Call"
    assert spoken(timeline, 110) == "Call SQL"
    assert spoken(timeline, 150) == "Call SQL now"


def test_partly_replaced_word_counts_as_unspoken_until_its_end() -> None:
    applier = PronunciationApplier([PronunciationRule("SQL", "sequel")])
    clause = applier.apply("SQL's fine")
    timeline = AlignmentTimeline(ULAW_8000)
    timeline.add_clause(clause)
    timeline.add_chunk(*aligned(clause.text, 10))
    assert spoken(timeline, 60) == ""
    assert spoken(timeline, 80) == "SQL's"


def test_alignment_may_omit_whitespace_and_extra_chars_are_ignored() -> None:
    timeline = AlignmentTimeline(ULAW_8000)
    timeline.add_clause("a b")
    timeline.add_chunk(b"\xff" * 400, Alignment(("a", "b", "c", "\n"), (0, 10, 20, 30), (10, 10, 10, 10)))
    assert timeline.aligned_chars == 2
    assert timeline.char_end_ms(0) == 10
    assert timeline.char_end_ms(1) is None
    assert timeline.char_end_ms(2) == 20
    assert timeline.char_end_ms(-1) is None
    assert spoken(timeline, 20) == "a b"


def test_pcm16_chunk_duration_uses_32_bytes_per_ms() -> None:
    timeline = AlignmentTimeline(PCM16LE_16000)
    timeline.add_clause("hi yo")
    timeline.add_chunk(b"\x00" * 32 * 50, Alignment(("h", "i"), (0, 10), (10, 10)))
    timeline.add_chunk(b"\x00" * 32 * 30, Alignment((" ", "y", "o"), (0, 10, 20), (10, 10, 10)))
    assert timeline.format is PCM16LE_16000
    assert timeline.offset_ms == 80
    assert timeline.char_end_ms(4) == 80


def test_unequal_alignment_arrays_are_rejected() -> None:
    timeline = AlignmentTimeline(ULAW_8000)
    timeline.add_clause("ab")
    with pytest.raises(ValueError, match="differ in length"):
        timeline.add_chunk(b"\xff" * 80, Alignment(("a", "b"), (0,), (5, 5)))


def test_gap_before_an_aligned_chunk_is_interpolated_linearly() -> None:
    timeline = AlignmentTimeline(ULAW_8000)
    timeline.add_clause("abcd efgh")
    timeline.add_chunk(b"\xff" * 8 * 40, None)
    timeline.add_chunk(b"\xff" * 8 * 40, None)
    tail_audio, tail = aligned("efgh", 10)
    timeline.add_chunk(tail_audio, tail)
    assert [timeline.char_end_ms(i) for i in range(4)] == [20, 40, 60, 80]
    assert timeline.char_end_ms(5) == 90
    assert spoken(timeline, 79) == ""
    assert spoken(timeline, 80) == "abcd"
    assert spoken(timeline, 120) == "abcd efgh"


def test_trailing_gap_spreads_the_rest_of_the_current_clause() -> None:
    timeline = AlignmentTimeline(ULAW_8000)
    timeline.add_clause("ab cd ")
    timeline.add_clause("ef ")
    head_audio, head = aligned("ab", 10)
    timeline.add_chunk(head_audio, head)
    timeline.add_chunk(b"\xff" * 8 * 40, None)
    assert timeline.aligned_chars == 2
    assert timeline.char_end_ms(3) == 40
    assert timeline.char_end_ms(4) == 60
    assert timeline.char_end_ms(6) is None
    assert spoken(timeline, 59) == "ab"
    assert spoken(timeline, 60) == "ab cd"
    assert spoken(timeline, 1_000) == "ab cd"


def test_gap_whose_aligned_follow_up_cannot_be_found_is_dropped() -> None:
    timeline = AlignmentTimeline(ULAW_8000)
    timeline.add_clause("abcd")
    timeline.add_chunk(b"\xff" * 8 * 20, None)
    timeline.add_chunk(b"\xff" * 8 * 20, Alignment(("x", "y"), (0, 10), (10, 10)))
    assert timeline.char_end_ms(0) == 30
    assert timeline.char_end_ms(1) == 40


def test_gap_followed_by_the_expected_char_skips_nothing() -> None:
    timeline = AlignmentTimeline(ULAW_8000)
    timeline.add_clause("ab")
    timeline.add_chunk(b"\xff" * 8 * 20, None)
    timeline.add_chunk(*aligned("ab", 10))
    assert timeline.char_end_ms(0) == 30


def test_whitespace_only_alignment_and_gaps_after_everything_is_timed() -> None:
    timeline = AlignmentTimeline(ULAW_8000)
    timeline.add_clause("ab ")
    timeline.add_chunk(*aligned("ab", 10))
    timeline.add_chunk(b"\xff" * 80, Alignment((" ",), (0,), (10,)))
    timeline.add_chunk(b"", None)
    timeline.add_chunk(b"\xff" * 80, None)
    timeline.add_chunk(*aligned("zz", 10))
    assert timeline.aligned_chars == 2
    assert timeline.char_end_ms(1) == 20
    assert spoken(timeline, 20) == "ab"


def test_empty_timeline() -> None:
    timeline = AlignmentTimeline(ULAW_8000)
    assert timeline.text_until(100) == SpokenPrefix("", 0)
    assert timeline.char_end_ms(0) is None
    assert timeline.total_chars == 0
    assert CharTiming(0, 1).end_ms == 1


_WORDS = st.text(alphabet="abcXYZ.,'!", min_size=1, max_size=6)


def _reference(words: list[str], ends: list[float], played_ms: float) -> str:
    kept: list[str] = []
    visible = 0
    for word in words:
        visible += len(word)
        if max(ends[:visible]) > played_ms:
            break
        kept.append(word)
    return " ".join(kept)


@settings(max_examples=150, deadline=None)
@given(
    words=st.lists(_WORDS, min_size=1, max_size=12),
    data=st.data(),
)
def test_text_until_matches_a_word_by_word_reference(words: list[str], data: st.DataObject) -> None:
    text = " ".join(words) + " "
    cuts = sorted(data.draw(st.sets(st.integers(1, len(text) - 1), max_size=4)))
    clauses = [text[a:b] for a, b in zip([0, *cuts], [*cuts, len(text)], strict=True)]
    visible_count = sum(len(word) for word in words)
    durations = data.draw(st.lists(st.integers(0, 40), min_size=visible_count, max_size=visible_count))
    timeline = AlignmentTimeline(ULAW_8000)
    ends: list[float] = []
    elapsed = 0
    for clause in clauses:
        timeline.add_clause(clause)
    for chunk in clauses:
        chars = [char for char in chunk if not char.isspace()]
        own = durations[len(ends) : len(ends) + len(chars)]
        starts = [sum(own[:i]) for i in range(len(own))]
        timeline.add_chunk(b"\xff" * 8 * sum(own), Alignment(tuple(chars), tuple(starts), tuple(own)))
        ends.extend(elapsed + start + duration for start, duration in zip(starts, own, strict=True))
        elapsed += sum(own)
    previous = ""
    for played_ms in sorted(data.draw(st.lists(st.integers(-10, elapsed + 10), max_size=8))):
        result = spoken(timeline, played_ms)
        assert result == _reference(words, ends, played_ms)
        assert text.startswith(result)
        assert len(result) >= len(previous)
        previous = result
