"""Realtime STT audio analysis (specs/30 §3.3; Decision P03-T05a rule 5): pure, synchronous, unit-testable.

Audio is cut into 20 ms frames; a frame is voiced when its RMS is at least -40 dBFS. Voiced frames group
into segments (gaps under 300 ms stay inside one); once a segment has 100 ms of voiced audio and its marker
window (40 ms pre-roll plus 260 ms) is complete, ``decode_marker`` runs once and the socket's resolver maps
the marker to the text heard. Words are revealed in proportion to voiced time over the utterance's expected
duration, all of them once the segment ends. The commit window is every revealed, uncommitted word; partials
fire every 300 ms of voiced audio in the window, and a ``vad`` stream commits by itself after the configured
silence.

``feed`` and ``commit`` are CPU work (marker decoding): the socket runs them in a worker thread, one at a
time.
"""

from __future__ import annotations

import base64
import binascii
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import numpy as np
from numpy.typing import NDArray

from va_sim.audio.codec import ulaw_decode
from va_sim.audio.marker import MARKER_MS, decode_marker
from va_sim.audio.synth import utterance_ms
from va_sim.elevenlabs.stt_params import SttFormat
from va_sim.elevenlabs.stt_text import CALLER_MS_PER_CHAR, TimedWord, timed_words

__all__ = [
    "FRAME_MS",
    "MARKER_WINDOW_MS",
    "MIN_SPEECH_MS",
    "PARTIAL_EVERY_MS",
    "PRE_ROLL_MS",
    "SEGMENT_GAP_MS",
    "UNINTELLIGIBLE",
    "VOICED_RMS",
    "Commit",
    "Heard",
    "Partial",
    "Resolver",
    "StreamEvent",
    "SttStream",
    "decode_base64",
    "decode_chunk",
]

FRAME_MS: Final = 20
VOICED_RMS: Final = 328  # -40 dBFS: round(32 767 · 10^(-40/20))
SEGMENT_GAP_MS: Final = 300
MIN_SPEECH_MS: Final = 100
PARTIAL_EVERY_MS: Final = 300
PRE_ROLL_MS: Final = 40
MARKER_WINDOW_MS: Final = 260
UNINTELLIGIBLE: Final = "[unintelligible]"
_MS_PER_S: Final = 1000


@dataclass(frozen=True, slots=True)
class Heard:
    """What a segment said: the resolver's answer for its decoded marker (or for no marker)."""

    text: str
    language: str
    utterance_id: int | None
    has_marker: bool


type Resolver = Callable[[int | None], Heard]


@dataclass(frozen=True, slots=True)
class Partial:
    text: str


@dataclass(frozen=True, slots=True)
class Commit:
    """A committed window: its text, timed words, language, contributing utterance ids; ``auto`` for vad."""

    text: str
    words: tuple[TimedWord, ...]
    language: str
    utterance_ids: tuple[int, ...]
    auto: bool = False


type StreamEvent = Partial | Commit


def decode_chunk(audio: bytes, fmt: SttFormat) -> NDArray[np.int16]:
    """int16 samples of ``audio`` in ``fmt``; ``ValueError`` for a PCM payload with an odd byte count."""
    if fmt.encoding == "ulaw":
        return ulaw_decode(audio)
    if len(audio) % 2:
        msg = "pcm audio must contain an even number of bytes"
        raise ValueError(msg)
    return np.frombuffer(audio, dtype="<i2").astype(np.int16)


def decode_base64(audio: str) -> bytes:
    """Strict base64 decoding; ``ValueError`` when ``audio`` is not valid base64."""
    try:
        return base64.b64decode(audio, validate=True)
    except binascii.Error:
        msg = "audio_base_64 is not valid base64"
        raise ValueError(msg) from None


@dataclass(slots=True, eq=False)
class _Segment:
    """A run of voiced frames (hashed by identity)."""

    start_ms: int
    marker_samples: list[NDArray[np.int16]]
    voiced_ms: int = 0
    window_samples: int = 0
    ended: bool = False
    heard: Heard | None = None
    words: tuple[TimedWord, ...] = ()
    expected_ms: int = 0
    committed: int = 0
    marker_done: bool = False

    def revealed(self) -> int:
        """How many of the words are heard so far."""
        if self.heard is None:
            return 0
        if self.ended:
            return len(self.words)
        return min(len(self.words), math.ceil(len(self.words) * self.voiced_ms / self.expected_ms))

    def pending(self) -> tuple[TimedWord, ...]:
        return self.words[self.committed : self.revealed()]

    def done(self) -> bool:
        """Ended with every word committed: nothing more can come from it."""
        return self.ended and self.committed == len(self.words)


class SttStream:
    """One session's audio analysis: frames, segments, markers, the commit window."""

    def __init__(
        self, sample_rate: int, resolver: Resolver, *, language: str, vad_silence_ms: int | None = None
    ) -> None:
        self._rate = sample_rate
        self._frame = sample_rate * FRAME_MS // _MS_PER_S
        self._window = sample_rate * MARKER_WINDOW_MS // _MS_PER_S
        self._resolver = resolver
        self._language = language
        self._vad_silence_ms = vad_silence_ms
        self._carry: NDArray[np.int16] = np.zeros(0, dtype=np.int16)
        self._pre_roll: deque[NDArray[np.int16]] = deque(maxlen=PRE_ROLL_MS // FRAME_MS)
        self._segments: list[_Segment] = []
        self._open: _Segment | None = None
        self._unvoiced_ms = 0
        self._window_voiced_ms = 0
        self.audio_ms = 0
        self.voiced_ms = 0

    def feed(self, pcm: NDArray[np.int16]) -> list[StreamEvent]:
        """Analyse more audio; the partials (and, for vad, the automatic commits) it produced, in order."""
        samples = np.concatenate([self._carry, pcm]) if self._carry.size else pcm
        whole = samples.size // self._frame * self._frame
        self._carry = samples[whole:].copy()
        events: list[StreamEvent] = []
        frames = samples[:whole].reshape(-1, self._frame)
        levels = np.sqrt(np.mean(np.square(frames.astype(np.float64)), axis=1))
        for frame, level in zip(frames, levels, strict=True):
            event = self._frame_event(frame, voiced=bool(level >= VOICED_RMS))
            if event is not None:
                events.append(event)
        return events

    def commit(self, *, auto: bool = False) -> Commit:
        """Commit every revealed, uncommitted word and start a new window.

        A segment still collecting its marker window contributes from a later commit on (decoding a partial
        preamble would misread it); ended segments are always resolved.
        """
        heard, words = self._take_window()
        self._window_voiced_ms = 0
        ids = tuple(dict.fromkeys(h.utterance_id for h in heard if h.utterance_id is not None))
        language = heard[0].language if heard else self._language
        return Commit(" ".join(word.text for word in words), words, language, ids, auto=auto)

    def _take_window(self) -> tuple[list[Heard], tuple[TimedWord, ...]]:
        """Mark the window's words committed; what the contributing segments heard, and the words."""
        heard: list[Heard] = []
        words: list[TimedWord] = []
        for segment in self._segments:
            pending = segment.pending()
            if pending and segment.heard is not None:
                heard.append(segment.heard)
                words.extend(pending)
                segment.committed += len(pending)
        self._segments = [segment for segment in self._segments if not segment.done()]
        return heard, tuple(words)

    def window_text(self) -> str:
        return " ".join(word.text for segment in self._segments for word in segment.pending())

    # Frames.

    def _frame_event(self, frame: NDArray[np.int16], *, voiced: bool) -> StreamEvent | None:
        start = self.audio_ms
        self.audio_ms += FRAME_MS
        segment = self._open
        if voiced:
            self._unvoiced_ms = 0
            if segment is None:
                segment = _Segment(start_ms=start, marker_samples=list(self._pre_roll))
                self._segments.append(segment)
                self._open = segment
            segment.voiced_ms += FRAME_MS
            self.voiced_ms += FRAME_MS
            self._window_voiced_ms += FRAME_MS
        else:
            self._unvoiced_ms += FRAME_MS
        self._pre_roll.append(frame)
        if segment is not None:
            self._collect(segment, frame)
            if self._unvoiced_ms >= SEGMENT_GAP_MS:
                self._end(segment)
        return self._event_after_frame(voiced=voiced)

    def _event_after_frame(self, *, voiced: bool) -> StreamEvent | None:
        if voiced and self._window_voiced_ms % PARTIAL_EVERY_MS == 0:
            text = self.window_text()
            return Partial(text) if text else None
        vad_ms = self._vad_silence_ms
        crossed = vad_ms is not None and self._unvoiced_ms >= vad_ms > self._unvoiced_ms - FRAME_MS
        if crossed and self._open is None:
            return self.commit(auto=True) if self.window_text() else None
        return None

    def _collect(self, segment: _Segment, frame: NDArray[np.int16]) -> None:
        if segment.marker_done:
            return
        if segment.window_samples < self._window:
            segment.marker_samples.append(frame)
            segment.window_samples += frame.size
        self._resolve(segment, force=False)

    def _end(self, segment: _Segment) -> None:
        segment.ended = True
        self._open = None
        if segment.voiced_ms < MIN_SPEECH_MS:
            self._segments.remove(segment)
            return
        self._resolve(segment, force=True)

    # Markers.

    def _resolve(self, segment: _Segment, *, force: bool) -> None:
        """Decode the marker and ask the resolver once the segment is long enough (or ``force``d)."""
        if segment.heard is not None or segment.voiced_ms < MIN_SPEECH_MS:
            return
        if not force and segment.window_samples < self._window:
            return
        samples = np.concatenate(segment.marker_samples)
        missing = self._window - segment.window_samples
        if missing > 0:
            samples = np.concatenate([samples, np.zeros(missing, dtype=np.int16)])
        segment.marker_samples = []
        segment.marker_done = True
        heard = self._resolver(decode_marker(samples, self._rate))
        segment.heard = heard
        if heard.has_marker:
            body_start = segment.start_ms + MARKER_MS
            segment.expected_ms = utterance_ms(heard.text, CALLER_MS_PER_CHAR)
        else:
            body_start = segment.start_ms
            segment.expected_ms = max(len(heard.text) * CALLER_MS_PER_CHAR, FRAME_MS)
        segment.words = timed_words(heard.text, body_start)
