"""Fixed texts shared by the barge-in and interruptibility flows (specs/30 §5, Decision P05-T17d)."""

from __future__ import annotations

from typing import Final

__all__ = ["LONG_FIRST_SENTENCE", "LONG_SENTENCE_CHARS", "TASK"]

LONG_SENTENCE_CHARS: Final = 300
# 300 characters: about 18 s of FakeElevenLabs speech at 60 ms per character.
LONG_FIRST_SENTENCE: Final = (
    "Hey, this is Maya from Bella Pasta on Market Street. I am calling about the dinner reservation you "
    "requested for this weekend, because our kitchen team wants to confirm the number of guests, "
    "any allergies we should know about, whether you prefer the patio or inside, "
    "and the time that suits you best."
)
TASK: Final = "You are Maya from Bella Pasta. Confirm the caller's weekend reservation details."
