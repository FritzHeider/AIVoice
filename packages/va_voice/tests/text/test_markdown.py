"""Markdown/whitespace normalization and its streaming twin (specs/09-tts-elevenlabs.md §5 step 1)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from text_helpers import split_at

from va_voice.text.markdown import MarkdownStripper, normalize_text

CASES = [
    ("**bold** text", "bold text"),
    ("an *em* word", "an em word"),
    ("an _em_ word", "an em word"),
    ("the snake_case_id field", "the snake_case_id field"),
    ("a __dunder__ name", "a dunder name"),
    ("x_ y", "x y"),
    ("run `code` now", "run code now"),
    ("# Title\nText", "Title Text"),
    ("### H", "H"),
    ("##\nnext", "next"),
    ("# # twice", "twice"),
    ("- a\n- b", "a b"),
    ("* a", "a"),
    ("+ a", "a"),
    ("  - indented", "indented"),
    ("1. a\n2. b", "a b"),
    ("12) x", "x"),
    ("## - heading then marker", "heading then marker"),
    ("#hashtag stays", "#hashtag stays"),
    ("say #1 now", "say #1 now"),
    ("-5 degrees", "-5 degrees"),
    ("15 people", "15 people"),
    ("1234. not a marker", "1234. not a marker"),
    ("3.14 is pi", "3.14 is pi"),
    ("15.\nnext", "15. next"),
    ("a - b", "a - b"),
    ("line one\r\nline two\rthree", "line one line two three"),
    ("tab\tseparated\u00a0nbsp", "tab separated nbsp"),
    ("   padded   ", "padded"),
    ("", ""),
    ("**", ""),
]


def stream(parts: list[str], stripper: MarkdownStripper | None = None) -> str:
    active = MarkdownStripper() if stripper is None else stripper
    return "".join(active.feed(part) for part in parts) + active.finish()


@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_normalize_text(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_stream_char_by_char_matches(raw: str, expected: str) -> None:
    assert stream(list(raw)) == expected
    assert stream([raw]) == expected


def test_finish_resets_for_reuse() -> None:
    stripper = MarkdownStripper()
    assert stream(["Hello ", "**there**\n- "], stripper) == "Hello there"
    assert stream(["  - next one"], stripper) == "next one"


def test_space_pending_tracks_trailing_whitespace() -> None:
    stripper = MarkdownStripper()

    def step(raw: str) -> tuple[str, bool]:
        return stripper.feed(raw), stripper.space_pending

    assert step("  ") == ("", False)
    assert step("Hi there.") == ("Hi there.", False)
    assert step("\n") == ("", True)
    assert step("- Next") == (" Next", False)


def test_undecided_line_start_is_held() -> None:
    stripper = MarkdownStripper()
    assert stripper.feed("Hello\n1") == "Hello"
    assert stripper.feed(".") == ""
    assert stripper.feed(" item") == " item"
    assert stripper.feed("\n12") == ""
    assert stripper.feed("3") == ""
    assert stripper.feed("4") == " 1234"


def test_underscore_run_waits_for_right_neighbour() -> None:
    stripper = MarkdownStripper()
    assert stripper.feed("user__") == "user"
    assert stripper.feed("id") == "__id"
    assert stripper.feed(" _") == ""
    assert stripper.feed("x") == " x"


MARKDOWN_TEXT = st.text(alphabet="ab1 _*#-+.)`\n\r\t\u00a0", max_size=60)


@settings(max_examples=500)
@given(MARKDOWN_TEXT, st.lists(st.integers(min_value=0, max_value=60), max_size=8))
def test_stream_equals_reference_for_any_split(raw: str, cuts: list[int]) -> None:
    assert stream(split_at(raw, cuts)) == normalize_text(raw)


@given(st.text(max_size=40))
def test_stream_equals_reference_for_any_text(raw: str) -> None:
    assert stream(list(raw)) == normalize_text(raw)
