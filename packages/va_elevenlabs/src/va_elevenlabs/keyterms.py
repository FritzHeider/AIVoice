"""Keyterm parsing, validation, merging, and the LLM glossary (specs/08 §6).

Scribe realtime keyterms carry no weight, so the boost only orders terms (and picks the glossary).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

__all__ = [
    "DEFAULT_BOOST",
    "GLOSSARY_BOOST",
    "REALTIME_KEYTERM_CAP",
    "Keyterm",
    "KeytermSet",
    "glossary_line",
    "merge_keyterms",
    "parse_keyword",
]

DEFAULT_BOOST: Final = 2
GLOSSARY_BOOST: Final = 5
REALTIME_KEYTERM_CAP: Final = 100
_BOOST_RANGE: Final = range(1, 11)
_MAX_CHARS: Final = 50
_MAX_WORDS: Final = 5
_FORBIDDEN: Final = frozenset("<>{}[]\\")


@dataclass(frozen=True, slots=True)
class Keyterm:
    text: str
    boost: int = DEFAULT_BOOST


@dataclass(frozen=True, slots=True)
class KeytermSet:
    """The terms to send (boost desc, then source order), the glossary terms (boost ≥ 5), and warnings."""

    terms: tuple[str, ...]
    glossary: tuple[str, ...]
    warnings: tuple[str, ...]


def _split_boost(item: str) -> tuple[str, int]:
    word, sep, suffix = item.rpartition(":")
    if not sep or not suffix.strip().lstrip("-").isdigit():
        return item, DEFAULT_BOOST
    boost = int(suffix)
    if boost not in _BOOST_RANGE:
        msg = f"boost must be between 1 and 10, got {boost}"
        raise ValueError(msg)
    return word, boost


def parse_keyword(item: str) -> Keyterm:
    """``word`` or ``word:boost`` (boost 1-10, default 2); an invalid term is a ``ValueError``."""
    word, boost = _split_boost(item.strip())
    text = " ".join(word.split())
    if not text:
        msg = "empty term"
        raise ValueError(msg)
    if len(text) >= _MAX_CHARS:
        msg = f"must be shorter than {_MAX_CHARS} characters"
        raise ValueError(msg)
    if len(text.split()) > _MAX_WORDS:
        msg = f"at most {_MAX_WORDS} words"
        raise ValueError(msg)
    if any(char in _FORBIDDEN for char in text):
        msg = "contains one of < > { } [ ] \\"
        raise ValueError(msg)
    return Keyterm(text, boost)


def merge_keyterms(sources: Sequence[Iterable[str]], *, cap: int = REALTIME_KEYTERM_CAP) -> KeytermSet:
    """Merge the sources in order: drop invalid terms (with a warning), deduplicate case-insensitively (the
    first position, the highest boost), sort by boost descending (stable), and keep at most ``cap``."""
    warnings: list[str] = []
    merged: dict[str, Keyterm] = {}
    for source in sources:
        for item in source:
            try:
                term = parse_keyword(item)
            except ValueError as exc:
                warnings.append(f"keyterm {item!r} dropped: {exc}")
                continue
            key = term.text.casefold()
            known = merged.get(key)
            if known is None:
                merged[key] = term
            elif term.boost > known.boost:
                merged[key] = Keyterm(known.text, term.boost)
    ordered = sorted(merged.values(), key=lambda term: -term.boost)
    if len(ordered) > cap:
        warnings.append(f"{len(ordered) - cap} keyterms over the limit of {cap} dropped")
        ordered = ordered[:cap]
    glossary = tuple(term.text for term in ordered if term.boost >= GLOSSARY_BOOST)
    return KeytermSet(tuple(term.text for term in ordered), glossary, tuple(warnings))


def glossary_line(glossary: Sequence[str]) -> str | None:
    """The system prompt line for the glossary terms (``None`` when there are none)."""
    if not glossary:
        return None
    return f"Important names and terms: {', '.join(glossary)}"
