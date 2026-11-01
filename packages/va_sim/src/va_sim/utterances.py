"""The synthetic caller utterance registry entry (specs/30-testing-and-evals.md §3.1)."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Utterance"]


@dataclass(frozen=True, slots=True)
class Utterance:
    """A synthetic caller utterance (specs/30 §3.1 registry entry)."""

    text: str
    language: str
    words: tuple[str, ...]
