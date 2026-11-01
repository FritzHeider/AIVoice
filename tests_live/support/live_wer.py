"""Word error rate for the live STT checks (specs/30 §7 Decision P03-T16 rule 5)."""

from __future__ import annotations

import re
from typing import Final

__all__ = ["normalise", "wer"]

_RIGHT_QUOTE: Final = chr(0x2019)
_APOSTROPHES: Final = re.compile(f"['{_RIGHT_QUOTE}]")
_NON_WORD: Final = re.compile(r"[^\w\s]|_")


def normalise(text: str) -> list[str]:
    """Lowercase words with punctuation removed (apostrophes join, other marks split)."""
    joined = _APOSTROPHES.sub("", text.lower())
    return _NON_WORD.sub(" ", joined).split()


def wer(reference: str, hypothesis: str) -> float:
    """Word-level Levenshtein distance over the reference word count; an empty reference scores 0.0 against an
    empty hypothesis and 1.0 otherwise."""
    ref, hyp = normalise(reference), normalise(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    previous = list(range(len(hyp) + 1))
    for i, word in enumerate(ref, start=1):
        current = [i]
        for j, other in enumerate(hyp, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (word != other)))
        previous = current
    return previous[-1] / len(ref)
