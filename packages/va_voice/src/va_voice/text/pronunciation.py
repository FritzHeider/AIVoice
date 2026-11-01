"""Pronunciation guide applier (specs/09-tts-elevenlabs.md §6, rules in the §6 Decision P04-T07).

All rules of a call are compiled into ONE alternation regex (longest ``word`` first), so a single
left-to-right pass replaces every match and a replacement is never re-matched by another rule.
:meth:`PronunciationApplier.apply` returns the text to send together with an :class:`OffsetMap` that maps
sent-text positions back to the original clause, which is what the transcript stores and what barge-in
truncation cuts.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "OffsetMap",
    "PronouncedText",
    "PronunciationApplier",
    "PronunciationRule",
    "PronunciationRuleLike",
    "Replacement",
    "spell_out",
]


class PronunciationRuleLike(Protocol):
    """Any object shaped like a specs/05 §4.4 ``PronunciationRule`` (pydantic models satisfy it)."""

    @property
    def word(self) -> str: ...

    @property
    def pronunciation(self) -> str: ...

    @property
    def case_sensitive(self) -> bool: ...

    @property
    def spaced(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class PronunciationRule:
    """One pronunciation guide entry, for callers without the API's pydantic model."""

    word: str
    pronunciation: str
    case_sensitive: bool = False
    spaced: bool = False


@dataclass(frozen=True, slots=True)
class Replacement:
    """One replaced span: ``sent[sent_start:sent_end]`` replaced ``original[original_start:original_end]``."""

    sent_start: int
    sent_end: int
    original_start: int
    original_end: int


class OffsetMap:
    """Maps positions of the sent (replaced) text back to the original text.

    Positions are boundaries between characters (``0`` .. ``len(text)``). Outside a replaced span a position
    maps one to one, shifted by the length differences of the spans before it. A position strictly inside a
    replaced span has no exact counterpart: :meth:`to_original` maps it to the span start (a partially spoken
    replacement counts as not spoken) and :meth:`to_original_ceil` to the span end.
    """

    __slots__ = ("_original_length", "_replacements", "_sent_length", "_starts")

    def __init__(
        self, original_length: int, sent_length: int, replacements: Sequence[Replacement] = ()
    ) -> None:
        self._original_length = original_length
        self._sent_length = sent_length
        self._replacements = tuple(replacements)
        self._starts = [replacement.sent_start for replacement in self._replacements]

    @classmethod
    def identity(cls, length: int) -> OffsetMap:
        """The map of a text that had nothing replaced."""
        return cls(length, length)

    @property
    def replacements(self) -> tuple[Replacement, ...]:
        """The replaced spans in text order."""
        return self._replacements

    @property
    def sent_length(self) -> int:
        """Length of the sent text."""
        return self._sent_length

    @property
    def original_length(self) -> int:
        """Length of the original text."""
        return self._original_length

    def to_original(self, index: int) -> int:
        """Original position of sent position ``index``; inside a replaced span, the span's original start."""
        return self._map(index, ceil=False)

    def to_original_ceil(self, index: int) -> int:
        """Original position of sent position ``index``; inside a replaced span, the span's original end."""
        return self._map(index, ceil=True)

    def _map(self, index: int, *, ceil: bool) -> int:
        if index < 0 or index > self._sent_length:
            message = f"index {index} is outside the sent text (length {self._sent_length})"
            raise ValueError(message)
        position = bisect_right(self._starts, index) - 1
        if position < 0:
            return index
        span = self._replacements[position]
        if index >= span.sent_end:
            return span.original_end + (index - span.sent_end)
        if index == span.sent_start:
            return span.original_start
        return span.original_end if ceil else span.original_start


@dataclass(frozen=True, slots=True)
class PronouncedText:
    """Result of :meth:`PronunciationApplier.apply`: what to send, what was written, and how they align."""

    text: str
    original: str
    offsets: OffsetMap


def spell_out(text: str) -> str:
    """The ``spaced`` form of ``text``: its non-whitespace characters joined by single spaces."""
    return " ".join(char for char in text if not char.isspace())


@dataclass(frozen=True, slots=True)
class _Spec:
    pronunciation: str
    spaced: bool


def _usable(rule: PronunciationRuleLike) -> bool:
    if not rule.word.strip():
        return False
    return rule.spaced or bool(rule.pronunciation)


def _group(rule: PronunciationRuleLike) -> str:
    escaped = re.escape(rule.word)
    body = escaped if rule.case_sensitive else f"(?i:{escaped})"
    return rf"((?<!\w){body}(?!\w))"


class PronunciationApplier:
    """Applies a call's pronunciation guide to clauses and static phrases.

    The combined pattern is compiled once per applier (build one per call). Rules with an empty or
    whitespace-only ``word``, and non-spaced rules with an empty ``pronunciation``, are ignored. Among rules
    of equal ``word`` length the earlier one comes first, so for a duplicated word the earlier rule wins.
    """

    __slots__ = ("_pattern", "_specs")

    def __init__(self, rules: Iterable[PronunciationRuleLike]) -> None:
        ordered = sorted((rule for rule in rules if _usable(rule)), key=lambda rule: -len(rule.word))
        self._specs = tuple(_Spec(rule.pronunciation, rule.spaced) for rule in ordered)
        self._pattern = re.compile("|".join(_group(rule) for rule in ordered)) if ordered else None

    @property
    def rule_count(self) -> int:
        """Number of rules in effect (ignored rules excluded)."""
        return len(self._specs)

    def __bool__(self) -> bool:
        return self._pattern is not None

    def replace(self, text: str) -> str:
        """``text`` with every rule applied (for static and cached phrases)."""
        return self.apply(text).text

    def apply(self, text: str) -> PronouncedText:
        """``text`` with every rule applied, plus the map back to the original positions."""
        if self._pattern is None:
            return PronouncedText(text, text, OffsetMap.identity(len(text)))
        pieces: list[str] = []
        replacements: list[Replacement] = []
        copied = 0
        sent = 0
        for match in self._pattern.finditer(text):
            start, end = match.span()
            pieces.append(text[copied:start])
            sent += start - copied
            replacement = self._replacement(match)
            pieces.append(replacement)
            replacements.append(Replacement(sent, sent + len(replacement), start, end))
            sent += len(replacement)
            copied = end
        if not replacements:
            return PronouncedText(text, text, OffsetMap.identity(len(text)))
        pieces.append(text[copied:])
        sent += len(text) - copied
        return PronouncedText("".join(pieces), text, OffsetMap(len(text), sent, replacements))

    def _replacement(self, match: re.Match[str]) -> str:
        index = match.lastindex
        if index is None:  # pragma: no cover - every alternative is one capturing group
            message = "pronunciation match without a rule group"
            raise RuntimeError(message)
        spec = self._specs[index - 1]
        if not spec.spaced:
            return spec.pronunciation
        spelled = spell_out(spec.pronunciation)
        return spelled or spell_out(match.group(0))
