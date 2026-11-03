"""Spoken English integers for variable extraction (specs/11 §5.5 Decision P07-T04 rule 5).

``parse_spoken_integer("a hundred and five")`` → ``105``; digit strings (``"1,200"``) and digits mixed with
scale words (``"5 thousand"``) work too. Anything that is not a well-formed number gives ``None``,
and so does a digit string longer than ``MAX_DIGITS`` digits (it might not fit a signed 64-bit integer, and
Python refuses to convert strings beyond 4 300 digits).
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["MAX_DIGITS", "parse_spoken_integer"]

MAX_DIGITS: Final = 18
"""Longest digit run accepted: every 18-digit integer fits a signed 64-bit integer."""

_DIGITS: Final = re.compile(r"[+-]?\d{1,3}(?:,\d{3})+|[+-]?\d+")
_SPLIT: Final = re.compile(r"[\s-]+")
_HUNDRED: Final = 100

_UNITS: Final[dict[str, int]] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}  # fmt: skip
_TENS: Final[dict[str, int]] = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90,
}  # fmt: skip
_SCALES: Final[dict[str, int]] = {"thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}
_NEGATIVE: Final = frozenset({"minus", "negative"})
_ARTICLES: Final = frozenset({"a", "an"})


def _digit_value(text: str) -> int | None:
    if _DIGITS.fullmatch(text) is None:
        return None
    digits = text.replace(",", "")
    if len(digits.lstrip("+-")) > MAX_DIGITS:
        return None
    return int(digits)


class _Group:
    """A number below one thousand being read word by word (``None`` once malformed)."""

    __slots__ = ("hundreds", "rest", "started")

    def __init__(self) -> None:
        self.hundreds = 0
        self.rest = 0
        self.started = False

    @property
    def value(self) -> int:
        return self.hundreds + self.rest

    def add_small(self, value: int) -> bool:
        """A unit, teen, or tens word; False when it cannot follow what came before."""
        if self.rest == 0 and (not self.started or self.hundreds):
            if value == 0 and self.hundreds:
                return False
            self.rest = value
            self.started = True
            return True
        if self.rest in _TENS.values() and 0 < value < 10:  # noqa: PLR2004
            self.rest += value
            return True
        return False

    def add_hundred(self) -> bool:
        """``hundred`` after 1-99 (``fifteen hundred`` = 1 500)."""
        if self.hundreds or not 0 < self.rest < _HUNDRED:
            return False
        self.hundreds = self.rest * _HUNDRED
        self.rest = 0
        return True


class _Reader:
    """Reads number words left to right; :meth:`feed` returns False on the first malformed word."""

    __slots__ = ("article", "group", "last_scale", "total")

    def __init__(self) -> None:
        self.total = 0
        self.last_scale = 0
        self.group = _Group()
        self.article = False

    def feed(self, part: str) -> bool:
        if part == "and":
            return bool(self.group.hundreds or self.last_scale) and not self.group.rest
        if part in _ARTICLES:
            return self._article()
        if part in _UNITS or part in _TENS:
            return not self.article and self.group.add_small(_UNITS.get(part, _TENS.get(part, 0)))
        if part == "hundred":
            self._take_article()
            return self.group.add_hundred()
        if part in _SCALES:
            return self._scale(_SCALES[part])
        return self._digits(part)

    def _article(self) -> bool:
        if self.group.started or self.article:
            return False
        self.article = True
        return True

    def _take_article(self) -> None:
        """``a``/``an`` before ``hundred`` or a scale word counts as one."""
        if self.article:
            self.group.rest, self.group.started, self.article = 1, True, False

    def _scale(self, scale: int) -> bool:
        self._take_article()
        if not self.group.started or self.group.value == 0 or (self.last_scale and scale >= self.last_scale):
            return False
        self.total += self.group.value * scale
        self.last_scale = scale
        self.group = _Group()
        return True

    def _digits(self, part: str) -> bool:
        digits = _digit_value(part)
        if digits is None or digits < 0 or self.group.started or self.article:
            return False
        self.group.rest, self.group.started = digits, True
        return True

    def value(self) -> int | None:
        if self.article or (not self.group.started and self.last_scale == 0):
            return None
        return self.total + self.group.value


def _words(parts: list[str]) -> int | None:
    reader = _Reader()
    if not all(reader.feed(part) for part in parts):
        return None
    return reader.value()


def parse_spoken_integer(text: str) -> int | None:
    """The integer ``text`` states (digits, spoken English, or both), else ``None``."""
    stripped = text.strip().lower().rstrip(".!?")
    digits = _digit_value(stripped)
    if digits is not None:
        return digits
    parts = [part for part in _SPLIT.split(stripped) if part]
    sign = 1
    if parts and parts[0] in _NEGATIVE:
        sign = -1
        parts = parts[1:]
    if not parts:
        return None
    value = _words(parts)
    return None if value is None else sign * value
