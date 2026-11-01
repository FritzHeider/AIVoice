"""Transcript normalization helpers (specs/07-media-pipeline.md §6, §7)."""

from __future__ import annotations

import pytest

from va_voice.text_norm import ends_sentence, ends_with_digit, normalized, tokens


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ()),
        ("   ", ()),
        ("Hello, World!", ("hello", "world")),
        ("Uh-huh.", ("uh", "huh")),
        ("I don't know", ("i", "don't", "know")),
        ("'quoted'", ("quoted",)),
        ("zip 90210", ("zip", "90210")),
        ("Café  au_lait", ("café", "au", "lait")),
        ("mm-hmm... OK?", ("mm", "hmm", "ok")),
    ],
)
def test_tokens(text: str, expected: tuple[str, ...]) -> None:
    assert tokens(text) == expected


def test_normalized_joins_tokens() -> None:
    assert normalized("  Thank   you!! ") == "thank you"
    assert normalized("...") == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Yes.", True),
        ("Is it?", True),
        ("Wow!  ", True),
        ('He said "stop."', True),
        ("(really?)", True),
        ("yes", False),
        ("and,", False),
        ("", False),
        ("3.5 dollars", False),
    ],
)
def test_ends_sentence(raw: str, expected: bool) -> None:
    assert ends_sentence(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("my zip is 90210", True),
        ("my zip is 90210.", True),
        ("call 555-1234 ", True),
        ("five", False),
        ("90210 please", False),
        ("", False),
        ("...", False),
        ("٣", False),  # non-ASCII digits are not phone/zip digits
    ],
)
def test_ends_with_digit(raw: str, expected: bool) -> None:
    assert ends_with_digit(raw) is expected
