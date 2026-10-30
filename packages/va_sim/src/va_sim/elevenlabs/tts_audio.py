"""Deterministic TTS audio (specs/30-testing-and-evals.md §3.3; Decision P03-T04).

Each character lasts 60 ms: whitespace is silence, any other character a sine at
``300 + (ord(c) mod 12)·25`` Hz with a -10 dBFS peak, phase 0 at the character start. Audio is encoded in
the requested ElevenLabs ``output_format`` and cut into 200 ms pieces whose alignment lists the characters
starting inside the piece (start times relative to the piece). ``mp3_44100_128`` is a stream of silent MPEG
frames of the same duration. Everything here is pure CPU work; async callers run it in a worker thread.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from types import MappingProxyType
from typing import Final, Literal

import numpy as np
from numpy.typing import NDArray

from va_sim.audio.codec import dbfs_peak, ulaw_encode
from va_sim.elevenlabs.mp3 import FRAME_SAMPLES, MP3_SAMPLE_RATE, frame_start_ms, silent_frames

__all__ = [
    "CHAR_MS",
    "CHUNK_MS",
    "OUTPUT_FORMATS",
    "TONE_CLASSES",
    "Alignment",
    "OutputFormat",
    "TtsChunk",
    "absolute_alignment",
    "char_frequency",
    "chunks",
    "duration_ms",
    "parse_output_format",
    "pcm_samples",
    "synthesize",
    "tone_class",
    "tone_class_frequencies",
]

CHAR_MS: Final = 60
CHUNK_MS: Final = 200
TONE_DBFS: Final = -10.0
_BASE_HZ: Final = 300
_STEP_HZ: Final = 25
TONE_CLASSES: Final = 12
_MS_PER_S: Final = 1000

type Codec = Literal["ulaw", "pcm", "mp3"]
type Alignment = dict[str, list[str] | list[int]]


@dataclass(frozen=True, slots=True)
class OutputFormat:
    """An ElevenLabs ``output_format``: codec, sample rate, and HTTP content type."""

    name: str
    codec: Codec
    sample_rate: int
    content_type: str

    @property
    def bytes_per_sample(self) -> int:
        return 2 if self.codec == "pcm" else 1


def _formats() -> dict[str, OutputFormat]:
    formats = {"ulaw_8000": OutputFormat("ulaw_8000", "ulaw", 8_000, "audio/basic")}
    for rate in (16_000, 22_050, 24_000, 44_100):
        formats[f"pcm_{rate}"] = OutputFormat(f"pcm_{rate}", "pcm", rate, "audio/pcm")
    formats["mp3_44100_128"] = OutputFormat("mp3_44100_128", "mp3", MP3_SAMPLE_RATE, "audio/mpeg")
    return formats


OUTPUT_FORMATS: Final[Mapping[str, OutputFormat]] = MappingProxyType(_formats())


@dataclass(frozen=True, slots=True)
class TtsChunk:
    """One 200 ms audio piece and its alignment (``None`` when no character starts inside it)."""

    audio: bytes
    alignment: Alignment | None


def parse_output_format(name: str) -> OutputFormat:
    """The format called ``name``; ``ValueError`` names the supported ones otherwise."""
    found = OUTPUT_FORMATS.get(name)
    if found is None:
        msg = f"unsupported output_format {name!r} (supported: {', '.join(OUTPUT_FORMATS)})"
        raise ValueError(msg)
    return found


def tone_class(char: str) -> int | None:
    """The tone class (0..11) of ``char``; ``None`` for whitespace (silence)."""
    if char.isspace():
        return None
    return ord(char) % TONE_CLASSES


def tone_class_frequencies() -> tuple[int, ...]:
    """The frequency in Hz of every tone class, by class."""
    return tuple(_BASE_HZ + k * _STEP_HZ for k in range(TONE_CLASSES))


def char_frequency(char: str) -> int | None:
    """Tone frequency of ``char`` in Hz; ``None`` for whitespace (silence)."""
    k = tone_class(char)
    return None if k is None else _BASE_HZ + k * _STEP_HZ


def duration_ms(text: str) -> int:
    return len(text) * CHAR_MS


def _char_samples(sample_rate: int) -> int:
    return sample_rate * CHAR_MS // _MS_PER_S


@cache
def _tone(freq_hz: int, sample_rate: int) -> NDArray[np.int16]:
    n = np.arange(_char_samples(sample_rate))
    wave = dbfs_peak(TONE_DBFS) * np.sin(2 * np.pi * freq_hz * n / sample_rate)
    tone: NDArray[np.int16] = np.round(wave).astype(np.int16)
    tone.flags.writeable = False
    return tone


def pcm_samples(text: str, sample_rate: int) -> NDArray[np.int16]:
    """The int16 samples of ``text`` at ``sample_rate``."""
    silence = np.zeros(_char_samples(sample_rate), dtype=np.int16)
    pieces = []
    for char in text:
        freq = char_frequency(char)
        pieces.append(silence if freq is None else _tone(freq, sample_rate))
    if not pieces:
        return np.zeros(0, dtype=np.int16)
    return np.concatenate(pieces)


def _encode(samples: NDArray[np.int16], fmt: OutputFormat) -> bytes:
    if fmt.codec == "ulaw":
        return ulaw_encode(samples)
    return bytes(samples.astype("<i2").tobytes())


def _mp3_frame_count(total_ms: int) -> int:
    """Frames covering ``total_ms``, and at least one frame starting in the last 200 ms piece."""
    if total_ms == 0:
        return 0
    covering = math.ceil(total_ms * MP3_SAMPLE_RATE / (FRAME_SAMPLES * _MS_PER_S))
    last_piece_start = (math.ceil(total_ms / CHUNK_MS) - 1) * CHUNK_MS
    first_in_last_piece = math.ceil(last_piece_start * MP3_SAMPLE_RATE / (FRAME_SAMPLES * _MS_PER_S))
    return max(covering, first_in_last_piece + 1)


def synthesize(text: str, fmt: OutputFormat) -> bytes:
    """The whole audio of ``text`` in ``fmt``."""
    if fmt.codec == "mp3":
        return silent_frames(0, _mp3_frame_count(duration_ms(text)))
    return _encode(pcm_samples(text, fmt.sample_rate), fmt)


def _piece_alignment(text: str, start_ms: int) -> Alignment | None:
    first = math.ceil(start_ms / CHAR_MS)
    last = min(len(text), math.ceil((start_ms + CHUNK_MS) / CHAR_MS))
    if first >= last:
        return None
    return {
        "chars": list(text[first:last]),
        "charStartTimesMs": [index * CHAR_MS - start_ms for index in range(first, last)],
        "charDurationsMs": [CHAR_MS] * (last - first),
    }


def _pcm_pieces(text: str, fmt: OutputFormat) -> list[bytes]:
    audio = synthesize(text, fmt)
    step = fmt.sample_rate * CHUNK_MS // _MS_PER_S * fmt.bytes_per_sample
    return [audio[offset : offset + step] for offset in range(0, len(audio), step)]


def _mp3_pieces(text: str) -> list[bytes]:
    frames = _mp3_frame_count(duration_ms(text))
    pieces: list[bytes] = []
    first = 0
    while first < frames:
        window_end = (len(pieces) + 1) * CHUNK_MS
        last = first
        while last < frames and frame_start_ms(last) < window_end:
            last += 1
        pieces.append(silent_frames(first, last - first))
        first = last
    return pieces


def chunks(text: str, fmt: OutputFormat) -> list[TtsChunk]:
    """The 200 ms pieces of ``text`` in ``fmt`` with per-piece relative alignment."""
    pieces = _mp3_pieces(text) if fmt.codec == "mp3" else _pcm_pieces(text, fmt)
    return [
        TtsChunk(audio=piece, alignment=_piece_alignment(text, index * CHUNK_MS))
        for index, piece in enumerate(pieces)
    ]


def absolute_alignment(text: str) -> dict[str, list[str] | list[float]]:
    """REST ``with-timestamps`` alignment: characters with absolute start/end times in seconds."""
    starts = [round(index * CHAR_MS / _MS_PER_S, 3) for index in range(len(text))]
    ends = [round((index + 1) * CHAR_MS / _MS_PER_S, 3) for index in range(len(text))]
    return {
        "characters": list(text),
        "character_start_times_seconds": starts,
        "character_end_times_seconds": ends,
    }
