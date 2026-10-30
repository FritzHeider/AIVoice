"""Committed-transcript helpers: synthetic word timings and regex entities (Decision P03-T05a rules 5, 6).

A word's timing assumes the caller's synthesis rate: each character of the utterance text lasts
``CALLER_MS_PER_CHAR`` from the utterance body start. Entities are found by regex only:
emails (``email_address``, categories ``pii``/``all``) and runs of 13 or more digits optionally separated by
single spaces or hyphens (``credit_card_number``, categories ``pci``/``all``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

__all__ = ["CALLER_MS_PER_CHAR", "TimedWord", "find_entities", "timed_words", "words_json"]

# The synthetic caller's default speaking rate (va_sim.audio.synth's rate_ms_per_char default).
CALLER_MS_PER_CHAR: Final = 70
_WORD: Final = re.compile(r"\S+")
_EMAIL: Final = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_CARD: Final = re.compile(r"\d(?:[ -]?\d){12,}")
_EMAIL_CATEGORIES: Final = frozenset({"pii", "all"})
_CARD_CATEGORIES: Final = frozenset({"pci", "all"})
_MS_PER_S: Final = 1000


@dataclass(frozen=True, slots=True)
class TimedWord:
    """A word with its session-relative audio time in milliseconds."""

    text: str
    start_ms: int
    end_ms: int


def timed_words(text: str, body_start_ms: int) -> tuple[TimedWord, ...]:
    """Every whitespace-separated word of ``text``, timed from its character offset (``text.split()``)."""
    return tuple(
        TimedWord(
            match.group(),
            body_start_ms + match.start() * CALLER_MS_PER_CHAR,
            body_start_ms + match.end() * CALLER_MS_PER_CHAR,
        )
        for match in _WORD.finditer(text)
    )


def _seconds(ms: int) -> float:
    return round(ms / _MS_PER_S, 3)


def words_json(words: Sequence[TimedWord]) -> list[dict[str, object]]:
    """``committed_transcript_with_timestamps`` ``words``: word entries alternating with spacing entries."""
    entries: list[dict[str, object]] = []
    for index, word in enumerate(words):
        if index:
            previous = words[index - 1]
            entries.append(_entry(" ", previous.end_ms, word.start_ms, "spacing"))
        entries.append(_entry(word.text, word.start_ms, word.end_ms, "word"))
    return entries


def _entry(text: str, start_ms: int, end_ms: int, kind: str) -> dict[str, object]:
    return {"text": text, "start": _seconds(start_ms), "end": _seconds(end_ms), "type": kind, "logprob": 0.0}


def find_entities(text: str, categories: Iterable[str]) -> list[dict[str, object]]:
    """Regex entities of ``text`` for the requested ``entity_detection`` categories, sorted by start."""
    wanted = frozenset(categories)
    found: list[tuple[re.Match[str], str]] = []
    if wanted & _EMAIL_CATEGORIES:
        found.extend((match, "email_address") for match in _EMAIL.finditer(text))
    if wanted & _CARD_CATEGORIES:
        found.extend((match, "credit_card_number") for match in _CARD.finditer(text))
    found.sort(key=lambda item: item[0].span())
    return [
        {
            "text": match.group(),
            "entity_type": entity_type,
            "start_char": match.start(),
            "end_char": match.end(),
        }
        for match, entity_type in found
    ]
