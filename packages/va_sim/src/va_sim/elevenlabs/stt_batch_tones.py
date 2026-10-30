"""Agent speech in batch STT: TTS tone decoding and matching against the generated texts (Decision P03-T05b
rule 5). Pure and synchronous; the route runs it in a worker thread.

FakeElevenLabs TTS speaks each character as a 60 ms tone of class ``ord(c) mod 12`` (whitespace is silence),
so a segment of agent audio reads back as a string over 12 letters. From the onset (the first sample at
least 64 in magnitude, minus one), consecutive 60 ms slots are classified: a quiet slot is a space (the next
onset is searched again after it, so gaps of any length resync); a voiced slot is the class with the largest
Goertzel power, and only counts when that class holds at least 60 % of the slot energy. One impure slot means
the segment is not agent speech.

The decoded string is looked up in the TTS records' generated texts mapped the same way (whitespace runs
become one space); the first record containing it wins, and its original characters give the words. When the
whole string matches nowhere, the longest word-aligned prefix that matches is taken and the rest is matched
again (back-to-back generations); runs that match nothing become ``[unintelligible]``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np
from numpy.typing import NDArray

from va_sim.audio.marker import goertzel_power
from va_sim.elevenlabs.stt_stream import UNINTELLIGIBLE, VOICED_RMS
from va_sim.elevenlabs.tts_audio import CHAR_MS, tone_class, tone_class_frequencies

__all__ = [
    "ONSET_LEVEL",
    "PURITY",
    "AgentText",
    "BatchWord",
    "TonedSlot",
    "decode_tones",
    "map_agent_texts",
    "match_agent_text",
    "unintelligible",
]

ONSET_LEVEL: Final = 64
PURITY: Final = 0.6
_WORD: Final = re.compile(r"\S+")
_MS_PER_S: Final = 1000


@dataclass(frozen=True, slots=True)
class TonedSlot:
    """One 60 ms slot: its tone class (``None`` for a quiet slot, a space) and its sample range."""

    tone: int | None
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class BatchWord:
    """A transcribed word with its file-relative times in seconds and per-character times."""

    text: str
    start_s: float
    end_s: float
    characters: tuple[tuple[str, float, float], ...]


@dataclass(frozen=True, slots=True)
class AgentText:
    """A TTS generated text and its tone-class string, with each class-string position's original index."""

    text: str
    classes: str
    index: tuple[int, ...]


def unintelligible(start_s: float, end_s: float) -> BatchWord:
    """The ``[unintelligible]`` word for audio that maps to no known text."""
    return BatchWord(UNINTELLIGIBLE, start_s, end_s, ((UNINTELLIGIBLE, start_s, end_s),))


def _letter(tone: int) -> str:
    return chr(ord("a") + tone)


def map_agent_texts(texts: Sequence[str]) -> tuple[AgentText, ...]:
    """Every generated text as a tone-class string (whitespace runs become one space)."""
    mapped: list[AgentText] = []
    for text in texts:
        classes: list[str] = []
        index: list[int] = []
        for position, char in enumerate(text):
            tone = tone_class(char)
            if tone is None and classes and classes[-1] == " ":
                continue
            classes.append(" " if tone is None else _letter(tone))
            index.append(position)
        mapped.append(AgentText(text, "".join(classes), tuple(index)))
    return tuple(mapped)


def _onset(samples: NDArray[np.int16], start: int, end: int) -> int | None:
    """One sample before the first sample of at least ``ONSET_LEVEL`` in ``[start, end)``, clamped."""
    loud = np.flatnonzero(np.abs(samples[start:end].astype(np.int32)) >= ONSET_LEVEL)
    if loud.size == 0:
        return None
    return max(start, start + int(loud[0]) - 1)


def _classify(window: NDArray[np.float64], rate: int, frequencies: tuple[int, ...]) -> int | None:
    """The slot's tone class, or ``None`` when no class holds ``PURITY`` of its energy."""
    energy = float(np.dot(window, window))
    powers = [goertzel_power(window, freq, rate) for freq in frequencies]
    best = int(np.argmax(powers))
    return best if powers[best] >= PURITY * energy else None


def decode_tones(samples: NDArray[np.int16], rate: int, start: int, end: int) -> list[TonedSlot] | None:
    """The slots of the segment ``[start, end)``, or ``None`` when it is not TTS tone audio."""
    slot = rate * CHAR_MS // _MS_PER_S
    frequencies = tone_class_frequencies()
    slots: list[TonedSlot] = []
    position: int | None = _onset(samples, start, end)
    while position is not None and position + slot <= end:
        window = samples[position : position + slot].astype(np.float64)
        if np.sqrt(np.mean(np.square(window))) < VOICED_RMS:
            slots.append(TonedSlot(None, position, position + slot))
            position = _onset(samples, position + slot, end)
            continue
        tone = _classify(window, rate, frequencies)
        if tone is None:
            return None
        slots.append(TonedSlot(tone, position, position + slot))
        position += slot
    if not any(item.tone is not None for item in slots):
        return None
    return slots


def _decoded_words(slots: Sequence[TonedSlot]) -> list[list[TonedSlot]]:
    """The toned slots grouped into words (runs between quiet slots)."""
    words: list[list[TonedSlot]] = [[]]
    for item in slots:
        if item.tone is None:
            if words[-1]:
                words.append([])
        else:
            words[-1].append(item)
    return [word for word in words if word]


def _needle(words: Sequence[list[TonedSlot]]) -> str:
    return " ".join("".join(_letter(item.tone) for item in word if item.tone is not None) for word in words)


def _find(needle: str, texts: Sequence[AgentText]) -> str | None:
    """The original text of the first generated text containing ``needle`` (earliest offset)."""
    for text in texts:
        found = text.classes.find(needle)
        if found >= 0:
            return text.text[text.index[found] : text.index[found + len(needle) - 1] + 1]
    return None


def _longest_match(
    words: Sequence[list[TonedSlot]], first: int, texts: Sequence[AgentText]
) -> tuple[int, str] | None:
    """The end (exclusive) of the longest run of decoded words from ``first`` found in a text, and its
    original characters."""
    for last in range(len(words), first, -1):
        original = _find(_needle(words[first:last]), texts)
        if original is not None:
            return last, original
    return None


def _seconds(sample: int, rate: int) -> float:
    return sample / rate


def _timed(original: str, slots: Sequence[TonedSlot], rate: int) -> list[BatchWord]:
    """The words of ``original``; its non-space characters align one to one with ``slots``."""
    words: list[BatchWord] = []
    cursor = 0
    for match in _WORD.finditer(original):
        chars = slots[cursor : cursor + len(match.group())]
        cursor += len(chars)
        characters = tuple(
            (char, _seconds(item.start, rate), _seconds(item.end, rate))
            for char, item in zip(match.group(), chars, strict=True)
        )
        words.append(BatchWord(match.group(), characters[0][1], characters[-1][2], characters))
    return words


def _unmatched(words: Sequence[list[TonedSlot]], rate: int) -> BatchWord:
    return unintelligible(_seconds(words[0][0].start, rate), _seconds(words[-1][-1].end, rate))


def match_agent_text(
    slots: Sequence[TonedSlot], texts: Sequence[AgentText], rate: int
) -> list[BatchWord] | None:
    """The words the slots spell in the generated ``texts``; ``None`` when no part of them matches."""
    words = _decoded_words(slots)
    result: list[BatchWord] = []
    pending: list[list[TonedSlot]] = []
    matched = False
    first = 0
    while first < len(words):
        found = _longest_match(words, first, texts)
        if found is None:
            pending.append(words[first])
            first += 1
            continue
        if pending:
            result.append(_unmatched(pending, rate))
            pending = []
        last, original = found
        result.extend(_timed(original, [item for word in words[first:last] for item in word], rate))
        matched = True
        first = last
    if pending:
        result.append(_unmatched(pending, rate))
    return result if matched else None
