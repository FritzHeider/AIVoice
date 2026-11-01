"""Clause chunker (specs/09-tts-elevenlabs.md §5 and its Decision P04-T06)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from text_helpers import split_at

from va_voice.text.chunker import ABBREVIATIONS, LENGTH_CAP, ClauseChunker, chunk_stream
from va_voice.text.markdown import normalize_text


def chunk(parts: list[str], chunker: ClauseChunker | None = None) -> list[str]:
    active = ClauseChunker() if chunker is None else chunker
    clauses: list[str] = []
    for part in parts:
        clauses.extend(active.feed(part))
    clauses.extend(active.flush())
    return clauses


GOLDEN = [
    (
        "Thanks for calling Acme Dental. How can I help you today?",
        ["Thanks for calling Acme Dental. ", "How can I help you today? "],
    ),
    ("Hi. I'm Sarah from Acme.", ["Hi. I'm Sarah from Acme. "]),
    ("Hi there. Yes.", ["Hi there. ", "Yes. "]),
    ("Hello there. Ok sure. Great, let's go.", ["Hello there. ", "Ok sure. Great, let's go. "]),
    ("Sure thing, my friend, I can do that.", ["Sure thing, my friend, ", "I can do that. "]),
    (
        "Sure thing, my friend, I can do that, no problem at all, really, truly.",
        ["Sure thing, my friend, ", "I can do that, no problem at all, ", "really, truly. "],
    ),
    (
        "Dr. Smith will see you at 3 p.m. tomorrow, ok.",
        ["Dr. Smith will see you at 3 p.m. tomorrow, ", "ok. "],
    ),
    ("Use a brush, e.g. a soft one. We ship to the U.S. today.", None),
    ("No. 5 Main St. is open.", ["No. 5 Main St. is open. "]),
    ("No. I can't do that today.", ["No. I can't do that today. "]),
    (
        "I said hello. No. Then I said no. Then we left.",
        ["I said hello. ", "No. Then I said no. ", "Then we left. "],
    ),
    (
        "It costs $1,250.50 total, which is 3.14 times more, at 10:30 today.",
        ["It costs $1,250.50 total, ", "which is 3.14 times more, at 10:30 today. "],
    ),
    ("Well... I think so. Okay then.", ["Well... I think so. ", "Okay then. "]),
    ("Let me check that... one moment please.", ["Let me check that... ", "one moment please. "]),
    (
        "I can help you \u2014 let me look that up for you right now.",
        ["I can help you \u2014 ", "let me look that up for you right now. "],
    ),
    (
        "I can help \u2014 let me look that up for you right now.",
        ["I can help \u2014 let me look that up for you right now. "],
    ),
    ("It is a well-known fact here.", ["It is a well-known fact here. "]),
    ("Yes I can do - that for you.", ["Yes I can do - ", "that for you. "]),
    ("Our office hours are: nine to five.", ["Our office hours are: ", "nine to five. "]),
    ("Our hours are: nine to five.", ["Our hours are: nine to five. "]),
    ("Call me at 555-1234, okay?", ["Call me at 555-1234, ", "okay? "]),
    (
        "**Sure!** Here are options:\n1. Cleaning\n2. Whitening",
        ["Sure! Here are options: ", "Cleaning Whitening "],
    ),
    ('He said "Mr. Jones is here." Then left.', ['He said "Mr. Jones is here." Then left. ']),
    ("Wait!Really? Yes.", ["Wait!Really? Yes. "]),
]


@pytest.mark.parametrize(("text", "expected"), GOLDEN)
def test_golden(text: str, expected: list[str] | None) -> None:
    whole = chunk([text])
    assert chunk(list(text)) == whole
    if expected is not None:
        assert whole == expected
    for clause in whole:
        assert clause.endswith(" ")
        assert not clause.endswith("  ")
        assert not clause.startswith(" ")


def test_abbreviations_never_split() -> None:
    clauses = chunk(["Use a brush, e.g. a soft one. We ship to the U.S. today."])
    assert clauses == ["Use a brush, e.g. a soft one. ", "We ship to the U.S. today. "]


def test_abbreviation_set_is_the_spec_set() -> None:
    assert len(ABBREVIATIONS) == 27
    assert {"e.g", "i.e", "a.m", "p.m", "u.s", "no", "approx"} <= ABBREVIATIONS


@pytest.mark.parametrize("word", sorted(ABBREVIATIONS - {"no"}))
def test_each_abbreviation_is_not_a_boundary(word: str) -> None:
    text = f"Then we went to see the ({word.upper()}. people there)"
    assert chunk([text]) == [normalize_text(text) + " "]


def test_no_before_number_waits_for_next_token() -> None:
    chunker = ClauseChunker()
    assert chunker.feed("Okay, I said hello. Then I said no.") == ["Okay, I said hello. "]
    assert chunker.feed(" ") == []
    assert chunker.pending == "Then I said no."
    assert chunker.feed("5 times.") == []
    assert chunker.flush() == ["Then I said no. 5 times. "]


def test_no_at_end_of_stream_is_a_sentence() -> None:
    chunker = ClauseChunker()
    assert chunker.feed("I said hello. Then I said no. ") == ["I said hello. "]
    assert chunker.flush() == ["Then I said no. "]


def test_undecided_terminator_is_held() -> None:
    chunker = ClauseChunker()
    assert chunker.feed("It is 3.") == []
    assert chunker.feed("14 now") == []
    assert chunker.feed(". Next") == ["It is 3.14 now. "]
    assert chunker.flush() == ["Next "]


def test_boundary_released_once_whitespace_arrives() -> None:
    chunker = ClauseChunker()
    assert chunker.feed("Hello there.") == []
    assert chunker.feed(" ") == ["Hello there. "]
    assert chunker.emitted_count == 1


def test_length_cap_cuts_at_last_space() -> None:
    text = " ".join(["word"] * 40)
    clauses = chunk([text])
    assert len(clauses[0]) <= LENGTH_CAP
    assert clauses[0] == " ".join(["word"] * 32) + " "
    assert "".join(clauses) == text + " "


def test_length_cap_hard_cut_without_spaces() -> None:
    clauses = chunk(["x" * 300])
    assert clauses == ["x" * LENGTH_CAP + " ", "x" * 140 + " "]


def test_punctuation_only_rest_is_dropped() -> None:
    chunker = ClauseChunker()
    assert chunker.feed("Hello there friend. ") == ["Hello there friend. "]
    assert chunker.feed(" ... )") == []
    assert chunker.flush() == []


def test_punctuation_only_cap_clause_is_dropped() -> None:
    assert chunk(["! " * 85 + "ok"]) == ["! " * 5 + "ok "]


@pytest.mark.parametrize("text", ["", "   ", "\n\n", "**", "- "])
def test_empty_input_emits_nothing(text: str) -> None:
    assert chunk([text]) == []


def test_flush_resets_first_clause_rules() -> None:
    chunker = ClauseChunker()
    assert chunk(["Hi there. Ok fine."], chunker) == ["Hi there. ", "Ok fine. "]
    assert chunker.emitted_count == 0
    assert chunker.pending == ""
    assert chunk(["Ok fine. Sure."], chunker) == ["Ok fine. ", "Sure. "]


async def test_chunk_stream_matches_feed_and_flush() -> None:
    text = "Sure thing, my friend. **Here** is what I found: nothing much."

    async def deltas() -> AsyncIterator[str]:
        for index in range(0, len(text), 3):
            yield text[index : index + 3]

    assert [clause async for clause in chunk_stream(deltas())] == chunk([text])
    supplied = ClauseChunker()
    assert [clause async for clause in chunk_stream(deltas(), supplied)] == chunk([text])
    assert supplied.emitted_count == 0


WORD = st.text(alphabet="abcdefghijKLMNOP0123456789", min_size=1, max_size=12)
NUMBER = st.sampled_from(["3.14", "$1,250.50", "10:30", "1,000", "e.g.", "p.m.", "No."])
TOKEN = st.one_of(
    st.tuples(WORD, st.sampled_from(["", "", "", ".", ",", ";", ":", "!", "?", "..."])).map("".join),
    NUMBER,
    st.sampled_from(["\u2014", "-"]),
)
SPACING = st.sampled_from([" ", " ", " ", "  ", "\n", "\t"])


@st.composite
def speech(draw: st.DrawFn) -> str:
    tokens = draw(st.lists(TOKEN, max_size=60))
    words = [draw(WORD), *tokens, draw(WORD)]
    return "".join(token + draw(SPACING) for token in words).strip()


@settings(max_examples=300)
@given(speech(), st.lists(st.integers(min_value=0, max_value=800), max_size=12))
def test_properties(text: str, cuts: list[int]) -> None:
    clauses = chunk(split_at(text, cuts))
    assert clauses == chunk([text])
    assert "".join(clauses) == normalize_text(text) + " "
    for clause in clauses:
        assert len(clause) <= LENGTH_CAP + 1
        assert len(clause) <= 200
    for number in ("3.14", "$1,250.50", "10:30", "1,000"):
        expected = normalize_text(text).split().count(number)
        assert sum(clause.split().count(number) for clause in clauses) == expected


@settings(max_examples=300)
@given(
    st.text(alphabet="ab1 _*#-+.,;:!?)`\n\u2014", max_size=200),
    st.lists(st.integers(min_value=0, max_value=200), max_size=10),
)
def test_split_invariance_for_any_text(text: str, cuts: list[int]) -> None:
    clauses = chunk(split_at(text, cuts))
    assert clauses == chunk([text])
    for clause in clauses:
        assert len(clause) <= LENGTH_CAP + 1
        assert any(char.isalnum() for char in clause)
