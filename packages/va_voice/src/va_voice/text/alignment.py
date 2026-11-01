"""Alignment timeline of one agent utterance (specs/09-tts-elevenlabs.md §7, rules in its Decision P04-T08).

The timeline collects the clauses sent to TTS (with their pronunciation :class:`OffsetMap`) and the audio
chunks the vendor returns. ElevenLabs character timings are relative to each audio chunk; the timeline adds
the running chunk offset to make them absolute and assigns them, in order, to the non-whitespace characters of
the sent text.
:meth:`AlignmentTimeline.text_until` answers "which whole words of the original text had been heard by
``played_ms``", which barge-in truncation stores as ``text_spoken``.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from va_voice.audio.formats import AudioFormat
from va_voice.text.pronunciation import OffsetMap, PronouncedText

__all__ = [
    "RESYNC_PROBE_CHARS",
    "AlignmentTimeline",
    "CharTiming",
    "ChunkAlignmentLike",
    "SpokenPrefix",
]

RESYNC_PROBE_CHARS: Final = 8
"""Visible characters of an aligned chunk searched for after a chunk without alignment."""


class ChunkAlignmentLike(Protocol):
    """Character timings of one audio chunk, relative to the chunk start (``ChunkAlignment``)."""

    @property
    def chars(self) -> Sequence[str]: ...

    @property
    def char_start_ms(self) -> Sequence[int]: ...

    @property
    def char_duration_ms(self) -> Sequence[int]: ...


@dataclass(frozen=True, slots=True)
class CharTiming:
    """Absolute playback interval of one sent character, in ms from the utterance's first audio byte."""

    start_ms: float
    end_ms: float


@dataclass(frozen=True, slots=True)
class SpokenPrefix:
    """The heard part of the original text: whole words, no trailing whitespace; ``chars == len(text)``."""

    text: str
    chars: int


@dataclass(frozen=True, slots=True)
class _Clause:
    sent_start: int
    original_start: int
    offsets: OffsetMap
    visible_end: int


@dataclass(frozen=True, slots=True)
class _Gap:
    start_ms: float
    end_ms: float


def _interpolate(gap: _Gap, count: int) -> list[CharTiming]:
    step = (gap.end_ms - gap.start_ms) / count
    return [CharTiming(gap.start_ms + i * step, gap.start_ms + (i + 1) * step) for i in range(count)]


def _word_boundary(text: str, position: int) -> int:
    """``position`` itself when it ends a word, else the start of the word it cuts."""
    if position >= len(text) or text[position].isspace():
        return position
    while position > 0 and not text[position - 1].isspace():
        position -= 1
    return position


class AlignmentTimeline:
    """Maps playback time to the original text of one utterance (one TTS context).

    Feed every clause with :meth:`add_clause` before its audio arrives, and every audio chunk of the context
    with :meth:`add_chunk`. All times are ms from the utterance's first audio byte, the same axis as
    ``PlaybackTracker.played_ms``.
    """

    __slots__ = (
        "_clauses",
        "_ends",
        "_fmt",
        "_gap",
        "_offset_ms",
        "_original",
        "_sent",
        "_timings",
        "_visible",
        "_visible_text",
    )

    def __init__(self, fmt: AudioFormat) -> None:
        self._fmt = fmt
        self._sent: list[str] = []
        self._original: list[str] = []
        self._clauses: list[_Clause] = []
        self._visible: list[int] = []
        self._visible_text: list[str] = []
        self._timings: list[CharTiming] = []
        self._ends: list[float] = []
        self._gap: _Gap | None = None
        self._offset_ms = 0.0

    @property
    def format(self) -> AudioFormat:
        """Format of the audio chunks (sets ``chunk_ms``)."""
        return self._fmt

    @property
    def offset_ms(self) -> float:
        """Total audio received so far, in ms."""
        return self._offset_ms

    @property
    def sent_text(self) -> str:
        """Everything sent to TTS, clauses concatenated."""
        return "".join(self._sent)

    @property
    def original_text(self) -> str:
        """The original (transcript) text of every clause, concatenated."""
        return "".join(self._original)

    @property
    def total_chars(self) -> int:
        """Length of :attr:`original_text`."""
        return sum(len(part) for part in self._original)

    @property
    def aligned_chars(self) -> int:
        """Visible sent characters with a known timing (interpolated gaps included once resolved)."""
        return len(self._timings)

    def add_clause(self, clause: PronouncedText | str) -> None:
        """Append one clause as sent to TTS (a plain string had nothing replaced)."""
        pronounced = (
            PronouncedText(clause, clause, OffsetMap.identity(len(clause)))
            if isinstance(clause, str)
            else clause
        )
        sent_start = sum(len(part) for part in self._sent)
        original_start = self.total_chars
        for index, char in enumerate(pronounced.text):
            if not char.isspace():
                self._visible.append(sent_start + index)
                self._visible_text.append(char)
        self._sent.append(pronounced.text)
        self._original.append(pronounced.original)
        self._clauses.append(_Clause(sent_start, original_start, pronounced.offsets, len(self._visible)))

    def add_chunk(self, audio: bytes, alignment: ChunkAlignmentLike | None) -> None:
        """Record one audio chunk and its (chunk-relative) character timings, when the vendor sent them."""
        chunk_ms = self._fmt.duration_ms(len(audio))
        start_ms = self._offset_ms
        self._offset_ms += chunk_ms
        if alignment is None:
            if chunk_ms > 0:
                gap_start = start_ms if self._gap is None else self._gap.start_ms
                self._gap = _Gap(gap_start, self._offset_ms)
            return
        timings = _visible_timings(alignment, start_ms)
        if not timings:
            return
        if self._gap is not None:
            self._resolve_gap(self._gap, "".join(char for char, _ in timings[:RESYNC_PROBE_CHARS]))
            self._gap = None
        room = len(self._visible) - len(self._timings)
        for _, timing in timings[:room]:
            self._append(timing)

    def char_end_ms(self, sent_index: int) -> float | None:
        """End time of the sent character at ``sent_index``; ``None`` for whitespace or not yet timed."""
        position = bisect_right(self._visible, sent_index) - 1
        if position < 0 or self._visible[position] != sent_index:
            return None
        if position < len(self._timings):
            return self._timings[position].end_ms
        pending = self._pending_timings()
        offset = position - len(self._timings)
        return pending[offset].end_ms if offset < len(pending) else None

    def text_until(self, played_ms: float) -> SpokenPrefix:
        """Largest original prefix ending at a word boundary whose characters all ended by ``played_ms``."""
        heard = self._heard_visible(played_ms)
        sent_end = (
            self._visible[heard] if heard < len(self._visible) else sum(len(part) for part in self._sent)
        )
        original = self.original_text
        cut = _word_boundary(original, self._to_original(sent_end))
        text = original[:cut].rstrip()
        return SpokenPrefix(text, len(text))

    def _append(self, timing: CharTiming) -> None:
        self._timings.append(timing)
        previous = self._ends[-1] if self._ends else timing.end_ms
        self._ends.append(max(previous, timing.end_ms))

    def _clause_of_visible(self, position: int) -> int:
        ends = [clause.visible_end for clause in self._clauses]
        return bisect_right(ends, position)

    def _resolve_gap(self, gap: _Gap, probe: str) -> None:
        cursor = len(self._timings)
        if cursor >= len(self._visible):
            return
        clause = self._clause_of_visible(cursor)
        limit = self._clauses[min(clause + 1, len(self._clauses) - 1)].visible_end
        found = "".join(self._visible_text).find(probe, cursor, limit + len(probe))
        if found <= cursor:
            return
        for timing in _interpolate(gap, found - cursor):
            self._append(timing)

    def _pending_timings(self) -> list[CharTiming]:
        """A trailing gap spread over the rest of the clause holding the next untimed character."""
        cursor = len(self._timings)
        if self._gap is None or cursor >= len(self._visible):
            return []
        end = self._clauses[self._clause_of_visible(cursor)].visible_end
        return _interpolate(self._gap, end - cursor)

    def _heard_visible(self, played_ms: float) -> int:
        heard = bisect_right(self._ends, played_ms)
        if heard < len(self._timings):
            return heard
        latest = self._ends[-1] if self._ends else float("-inf")
        for timing in self._pending_timings():
            latest = max(latest, timing.end_ms)
            if latest > played_ms:
                break
            heard += 1
        return heard

    def _to_original(self, sent_index: int) -> int:
        if not self._clauses:
            return 0
        starts = [clause.sent_start for clause in self._clauses]
        clause = self._clauses[max(bisect_right(starts, sent_index) - 1, 0)]
        return clause.original_start + clause.offsets.to_original(sent_index - clause.sent_start)


def _visible_timings(alignment: ChunkAlignmentLike, offset_ms: float) -> list[tuple[str, CharTiming]]:
    chars, starts, durations = alignment.chars, alignment.char_start_ms, alignment.char_duration_ms
    if not len(chars) == len(starts) == len(durations):
        message = (
            f"alignment arrays differ in length: chars={len(chars)}, starts={len(starts)}, "
            f"durations={len(durations)}"
        )
        raise ValueError(message)
    timings: list[tuple[str, CharTiming]] = []
    for char, start, duration in zip(chars, starts, durations, strict=True):
        if not char or char.isspace():
            continue
        absolute = offset_ms + start
        timings.append((char, CharTiming(absolute, absolute + duration)))
    return timings
