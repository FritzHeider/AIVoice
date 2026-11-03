"""Spoken English integers (specs/11 §5.5 Decision P07-T04 rule 5)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from va_engine.spoken_numbers import parse_spoken_integer

ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
    "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
)  # fmt: skip
TENS = ("_", "_", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
SCALES = ((1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand"))


def below_thousand(n: int, *, hyphen: bool) -> list[str]:
    words: list[str] = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        words += [ONES[hundreds], "hundred"]
        if rest:
            words.append("and")
    if rest >= 20:
        tens, ones = divmod(rest, 10)
        if ones and hyphen:
            words.append(f"{TENS[tens]}-{ONES[ones]}")
        else:
            words += [TENS[tens], *([ONES[ones]] if ones else [])]
    elif rest or not words:
        words.append(ONES[rest])
    return words


def spoken(n: int, *, hyphen: bool = False) -> str:
    """An independent reference speller: ``1205`` → ``one thousand two hundred and five``."""
    if n == 0:
        return "zero"
    words: list[str] = []
    for value, name in SCALES:
        count, n = divmod(n, value)
        if count:
            words += [*below_thousand(count, hyphen=hyphen), name]
    if n:
        words += below_thousand(n, hyphen=hyphen)
    return " ".join(words)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("twenty five", 25),
        ("Twenty-Five.", 25),
        ("a hundred and five", 105),
        ("a thousand", 1000),
        ("an hundred", 100),
        ("fifteen hundred", 1500),
        ("one thousand and one", 1001),
        ("5 thousand", 5000),
        ("two million 300 thousand", 2_300_000),
        ("1,200", 1200),
        ("+42", 42),
        ("minus three", -3),
        ("negative 4", -4),
        ("zero", 0),
        ("", None),
        ("minus", None),
        ("a", None),
        ("and", None),
        ("hundred", None),
        ("thousand", None),
        ("zero five", None),
        ("five twenty", None),
        ("twenty zero", None),
        ("twenty twenty", None),
        ("zero thousand", None),
        ("one hundred zero", None),
        ("two hundred hundred", None),
        ("two thousand million", None),
        ("thousand thousand", None),
        ("a five", None),
        ("a a hundred", None),
        ("five a", None),
        ("three 4", None),
        ("and five", None),
        ("twenty and", None),
        ("1,20", None),
        ("12abc", None),
        ("a few", None),
        ("123456789012345678", 123456789012345678),
        ("1234567890123456789", None),
        ("9" * 5000, None),
        ("1,000" * 1700, None),
        ("9" * 5000 + " thousand", None),
    ],
)
def test_parse(text: str, expected: int | None) -> None:
    assert parse_spoken_integer(text) == expected


@given(st.integers(min_value=0, max_value=999_999_999_999), st.booleans())
def test_reference_spelling_round_trips(n: int, hyphen: bool) -> None:
    assert parse_spoken_integer(spoken(n, hyphen=hyphen)) == n


@given(st.integers(min_value=-(10**15), max_value=10**15))
def test_digits_round_trip(n: int) -> None:
    assert parse_spoken_integer(str(n)) == n
    assert parse_spoken_integer(f"{n:,}") == n


@given(st.text(max_size=40))
def test_never_raises(text: str) -> None:
    result = parse_spoken_integer(text)
    assert result is None or isinstance(result, int)
