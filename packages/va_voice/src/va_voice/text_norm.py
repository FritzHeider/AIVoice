"""Transcript normalization shared by endpointing and backchannels (specs/07-media-pipeline.md §6).

Tokens are runs of letters/digits (any script), optionally joined by inner apostrophes (``don't``),
lowercased. Everything else (punctuation, hyphens, whitespace) separates tokens:
``"Uh-huh!"`` → ``("uh", "huh")``.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["ends_sentence", "ends_with_digit", "normalized", "tokens"]

_TOKEN: Final = re.compile(r"[^\W_]+(?:'[^\W_]+)*")
_LAST_ALNUM: Final = re.compile(r"([^\W_])[\W_]*\Z")
_CLOSERS: Final = "\"')]}\u201d\u2019\u00bb"  # closing quotes/brackets that may follow a sentence terminator
_TERMINATORS: Final = (".", "?", "!")
_ASCII_DIGITS: Final = frozenset("0123456789")


def tokens(text: str) -> tuple[str, ...]:
    """Lowercase word tokens of ``text`` in order."""
    return tuple(_TOKEN.findall(text.lower()))


def normalized(text: str) -> str:
    """``text`` lowercased and punctuation-stripped: its tokens joined by single spaces."""
    return " ".join(tokens(text))


def ends_sentence(raw: str) -> bool:
    """True when the raw partial ends with ``.``, ``?`` or ``!`` (trailing spaces/closing quotes ignored)."""
    return raw.rstrip().rstrip(_CLOSERS).endswith(_TERMINATORS)


def ends_with_digit(raw: str) -> bool:
    """True when the last letter-or-digit of ``raw`` is an ASCII digit (``"zip is 90210."`` → True)."""
    match = _LAST_ALNUM.search(raw)
    return match is not None and match.group(1) in _ASCII_DIGITS
