"""BargeInMonitor and merge_turns (pure; Decision P04-T12b)."""

from __future__ import annotations

from va_voice.pipeline import BargeInMonitor, UserTurn, merge_turns
from va_voice.vad import VadEvent


def start(at_ms: float) -> VadEvent:
    return VadEvent("speech_start", at_ms, 1.0, speech=True)


def end(at_ms: float) -> VadEvent:
    return VadEvent("speech_end", at_ms, 0.0, speech=False)


def snapshot(monitor: BargeInMonitor) -> tuple[str, bool]:
    """(partial, overlapped) read through a call, so mypy does not narrow the properties across asserts."""
    return monitor.partial, monitor.overlapped_playback


class Counter:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


def test_nothing_is_decided_while_silent_or_not_playing() -> None:
    unhonored = Counter()
    monitor = BargeInMonitor(on_unhonored=unhonored)
    assert monitor.evaluate(100.0, playing=True, level=4) is None
    monitor.on_vad(start(100.0))
    assert monitor.evaluate(1000.0, playing=False, level=4) is None
    assert unhonored.count == 0
    assert monitor.speech_started_ms == 100.0


def test_speech_ms_counts_from_the_segment_start_and_decides_once() -> None:
    monitor = BargeInMonitor()
    monitor.on_vad(start(1000.0))
    assert monitor.evaluate(1149.0, playing=True, level=4) is None
    decision = monitor.evaluate(1150.0, playing=True, level=4)
    assert decision is not None
    assert decision.reason == "any_speech"
    assert monitor.evaluate(1300.0, playing=True, level=4) is None
    # A repeated start inside the segment keeps the original start.
    monitor.on_vad(start(1200.0))
    assert monitor.speech_started_ms == 1000.0


def test_a_new_segment_can_interrupt_again() -> None:
    monitor = BargeInMonitor()
    monitor.on_vad(start(0.0))
    assert monitor.evaluate(200.0, playing=True, level=4)
    monitor.on_vad(end(300.0))
    assert monitor.speech_started_ms is None
    monitor.on_vad(start(500.0))
    assert monitor.evaluate(700.0, playing=True, level=4)


def test_partials_and_words_follow_the_level_table() -> None:
    monitor = BargeInMonitor()
    monitor.on_vad(start(0.0))
    monitor.on_partial("yeah")
    monitor.on_partial("   ")
    assert monitor.evaluate(600.0, playing=True, level=2) is None
    assert snapshot(monitor) == ("yeah", True)
    monitor.on_partial("hold on please")
    decision = monitor.evaluate(620.0, playing=True, level=2)
    assert decision is not None
    assert decision.reason == "speech_and_words"
    assert snapshot(monitor) == ("hold on please", False)
    monitor.reset_turn()
    assert snapshot(monitor) == ("", False)


def test_level0_speech_is_an_unhonored_attempt_but_not_an_overlap() -> None:
    unhonored = Counter()
    monitor = BargeInMonitor(on_unhonored=unhonored)
    monitor.on_vad(start(0.0))
    assert monitor.evaluate(2000.0, playing=True, level=0) is None
    assert not monitor.overlapped_playback
    monitor.on_vad(end(2100.0))
    assert unhonored.count == 1


def test_unhonored_is_counted_once_per_attempt() -> None:
    unhonored = Counter()
    monitor = BargeInMonitor(on_unhonored=unhonored)
    monitor.on_vad(start(0.0))
    assert monitor.evaluate(100.0, playing=True, level=2) is None
    # Playback ends while the caller keeps talking: counted now, once ...
    assert monitor.evaluate(120.0, playing=False, level=0) is None
    assert monitor.evaluate(140.0, playing=False, level=0) is None
    assert unhonored.count == 1
    monitor.on_vad(end(300.0))
    assert unhonored.count == 1
    # Speech that never overlapped playback is not an attempt.
    monitor.on_vad(start(3000.0))
    monitor.on_vad(end(3500.0))
    assert unhonored.count == 1


def test_a_segment_outliving_playback_is_evaluated_against_the_next_utterance() -> None:
    unhonored = Counter()
    monitor = BargeInMonitor(on_unhonored=unhonored)
    monitor.on_vad(start(0.0))
    # Level 0 line: an attempt, not honored ...
    assert monitor.evaluate(400.0, playing=True, level=0) is None
    # ... the line ends while the caller keeps talking: unhonored once ...
    assert monitor.evaluate(500.0, playing=False, level=0) is None
    assert unhonored.count == 1
    # ... and the same segment interrupts the next (interruptible) line.
    decision = monitor.evaluate(1000.0, playing=True, level=2)
    assert decision is not None
    assert decision.reason == "long_speech"
    assert monitor.evaluate(1020.0, playing=True, level=2) is None
    monitor.on_vad(end(1200.0))
    assert unhonored.count == 1


def test_honored_segments_are_not_unhonored() -> None:
    unhonored = Counter()
    monitor = BargeInMonitor(on_unhonored=unhonored)
    monitor.on_vad(start(0.0))
    assert monitor.evaluate(200.0, playing=True, level=4)
    monitor.on_vad(end(400.0))
    assert unhonored.count == 0
    assert monitor.classifier.is_backchannel("uh-huh")


def turn(index: int, text: str, start_ms: float, end_ms: float, **extra: object) -> UserTurn:
    words = tuple(text.split())
    return UserTurn(index, text, start_ms, end_ms, words, **extra)  # type: ignore[arg-type]


def test_merge_turns_concatenates_text_words_and_times() -> None:
    prev = turn(0, "I want to book", 100.0, 900.0, language_code="en")
    new = turn(2, "for Tuesday", 1500.0, 2200.0)
    merged = merge_turns(prev, new)
    assert merged == UserTurn(
        2, "I want to book for Tuesday", 100.0, 2200.0, ("I", "want", "to", "book", "for", "Tuesday"), "en"
    )
    assert merge_turns(prev, new, supersede=True).supersedes == 0


def test_merge_turns_keeps_new_when_there_is_nothing_in_front() -> None:
    new = turn(3, "hello", 10.0, 20.0, language_code="de")
    assert merge_turns(None, new) is new
    assert merge_turns(turn(1, "  ", 0.0, 5.0), new, supersede=True) is new
    merged = merge_turns(turn(1, "hi", 0.0, 5.0, language_code="en"), new)
    assert merged.language_code == "de"
