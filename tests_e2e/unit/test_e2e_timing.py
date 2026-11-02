"""Pure timing helpers of the barge-in and interruptibility flows (no stack)."""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest
from e2e_timing import (
    INTERRUPT_CATEGORY,
    Window,
    assistant_before,
    clears_in,
    first_clear_after,
    interrupt_events,
    utterance_window,
    whole_word_prefix,
)

from va_events import DEFAULT_CATEGORY

TIMELINE = {"clears": [900, 2_000, 2_450, 5_000]}


def test_the_interrupt_category_belongs_to_one_event_type() -> None:
    assert Counter(DEFAULT_CATEGORY.values())[INTERRUPT_CATEGORY] == 1


def test_utterance_windows_add_the_tail_and_need_an_end() -> None:
    assert utterance_window({"start_ms": 1_000, "end_ms": 1_600}, 500) == Window(1_000, 2_100)
    with pytest.raises(AssertionError, match="not ended"):
        utterance_window({"start_ms": 1_000, "end_ms": None}, 500)


def test_windows_are_half_open() -> None:
    window = Window(2_000, 2_450)
    assert 2_000 in window
    assert 2_450 not in window
    assert "2100" not in window


def test_clears_in_a_window_and_after_a_time() -> None:
    assert clears_in(TIMELINE, Window(1_000, 2_451)) == [2_000, 2_450]
    assert clears_in(TIMELINE, Window(3_000, 4_000)) == []
    assert first_clear_after(TIMELINE, 2_001) == 2_450
    assert first_clear_after(TIMELINE, 2_000) == 2_000
    assert first_clear_after(TIMELINE, 5_001) is None


def test_interrupt_events_select_the_interrupt_rows() -> None:
    rows: list[dict[str, Any]] = [
        {"category": INTERRUPT_CATEGORY, "data": {"played_ms": 1_300}},
        {"category": "latency", "data": {}},
    ]
    assert interrupt_events(rows) == rows[:1]


def test_assistant_before_finds_the_message_preceding_the_user_text() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "Hi, this is—"},
        {"role": "user", "content": "Wait, a question."},
        {"role": "assistant", "content": "Sure."},
    ]
    assert assistant_before(messages, "a question") == "Hi, this is—"
    assert assistant_before(messages, "absent") is None
    assert assistant_before([{"role": "user", "content": "Wait"}], "Wait") is None
    assert (
        assistant_before([{"role": "system", "content": "s"}, {"role": "user", "content": "Wait"}], "Wait")
        is None
    )


def test_whole_word_prefix_stops_at_the_last_complete_word() -> None:
    text = "Hey, this is Maya"
    assert whole_word_prefix(text, 3.9) == 0
    assert whole_word_prefix(text, 4) == 4
    assert whole_word_prefix(text, 8.5) == 4
    assert whole_word_prefix(text, 9) == 9
    assert whole_word_prefix(text, 16) == 12
    assert whole_word_prefix(text, 17) == 17
    assert whole_word_prefix(text, 40) == 17
