"""FakeLLM heuristic text helpers (specs/30 §3.4 Decision P03-T06b rule 4)."""

from __future__ import annotations

from typing import Any

import pytest
from llm_helpers import chat_body
from llm_schemas import router_user, transcript_user

from va_sim.llm.params import ChatRequest
from va_sim.llm.text import (
    STOPWORDS,
    Turn,
    caller_text,
    content_words,
    first_integer,
    first_number,
    first_words,
    latest_caller,
    name_value,
    overlap,
    transcript_text,
    trimmed,
    turns,
    words,
    yes_no,
)


def request(*texts: str, **extra: Any) -> ChatRequest:
    return ChatRequest.model_validate(chat_body(*texts, **extra))


def test_words_stopwords_and_overlap() -> None:
    assert words("I'm booking a Table, for 4!") == ["i'm", "booking", "a", "table", "for", "4"]
    assert content_words("Yes, I want the table") == {"yes", "want", "table"}
    assert {"yes", "no"}.isdisjoint(STOPWORDS)
    assert overlap("book a table", "Could I book the table?") == 2
    assert overlap("the a", "the a") == 0


@pytest.mark.parametrize(
    ("text", "integer", "number"),
    [
        ("4 people", 4, 4),
        ("we are twelve, maybe 13", 12, 12),
        ("about 2.5 hours", 2, 2.5),
        ("minus -3 degrees", 3, -3),
        ("nothing here", None, None),
    ],
)
def test_numbers(text: str, integer: int | None, number: float | None) -> None:
    assert first_integer(text) == integer
    assert first_number(text) == number


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Yeah sure", True), ("nope, yes", False), ("Absolutely.", True), ("nah", False), ("maybe", None)],
)
def test_yes_no(text: str, expected: bool | None) -> None:
    assert yes_no(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hi, my name is Dana Smith.", "Dana Smith"),
        ("this is Lee, calling back", "Lee"),
        ("I'm Ana", "Ana"),
        ("It is me!", "me"),
        ("hello", None),
    ],
)
def test_names(text: str, expected: str | None) -> None:
    assert name_value(text) == expected


def test_trimming() -> None:
    assert trimmed("  " + "x" * 100) == "x" * 80
    assert first_words("a b  c\nd", 3) == "a b c"


def test_router_layout_sources() -> None:
    req = request(router_user("book a table", "hello", "I'm Dana"))
    assert latest_caller(req) == "book a table"
    assert caller_text(req) == "hello\nI'm Dana\nbook a table"
    plain = request("first", "second")
    assert latest_caller(plain) == "second"
    assert caller_text(plain) == "first\nsecond"
    assert latest_caller(ChatRequest.model_validate(chat_body(system="s"))) == ""


def test_transcript_sources() -> None:
    req = request(transcript_user("user: hi there\nassistant: hello\n[T3|0-1|caller] yes"))
    assert transcript_text(req) == "hi there\nhello\nyes"
    assert caller_text(req) == "hi there"
    dialogue = ChatRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "Caller: one"},
                {"role": "assistant", "content": "two"},
            ],
        }
    )
    assert transcript_text(dialogue) == "one\ntwo"


def test_citation_turns() -> None:
    req = request("[T0|0-1200|agent] Hi.\n[T1|1300-4100|caller]  We are four.\nnoise")
    assert turns(req) == [Turn(0, "Hi."), Turn(1, "We are four.")]
