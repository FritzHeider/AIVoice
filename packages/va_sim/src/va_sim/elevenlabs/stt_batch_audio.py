"""Batch STT audio analysis (specs/30 §3.3; Decision P03-T05b rules 2, 4-7): pure and synchronous.

The upload is a 16-bit PCM WAV (or raw ``pcm_s16le_16``). Each channel is cut into voiced segments with the
realtime analysis constants (20 ms frames voiced at -40 dBFS, segments split by 300 ms of silence, at least
100 ms of voiced audio). A segment is agent speech when it decodes as TTS tones (``stt_batch_tones``);
otherwise it is caller speech, resolved through its utterance marker. Anything else is ``[unintelligible]``.
Batch transcription never consumes the realtime transcript script.
"""

from __future__ import annotations

import io
import wave
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

import numpy as np
from numpy.typing import NDArray

from va_sim.audio.marker import MARKER_MS, decode_marker
from va_sim.elevenlabs.stt_batch_tones import (
    AgentText,
    BatchWord,
    decode_tones,
    map_agent_texts,
    match_agent_text,
    unintelligible,
)
from va_sim.elevenlabs.stt_stream import (
    FRAME_MS,
    MARKER_WINDOW_MS,
    MIN_SPEECH_MS,
    PRE_ROLL_MS,
    SEGMENT_GAP_MS,
    VOICED_RMS,
)
from va_sim.elevenlabs.stt_text import CALLER_MS_PER_CHAR, timed_words

__all__ = [
    "MAX_CHANNELS",
    "MIN_AUDIO_MS",
    "PCM_S16LE_16_RATE",
    "BatchAudio",
    "BatchAudioError",
    "BatchSegment",
    "read_batch_audio",
    "transcribe",
    "transcribe_channel",
    "voiced_segments",
]

MAX_CHANNELS: Final = 5
MIN_AUDIO_MS: Final = 100
MIN_SAMPLE_RATE: Final = 8_000
PCM_S16LE_16_RATE: Final = 16_000
INVALID_AUDIO: Final = "Only 16-bit PCM WAV or pcm_s16le_16 audio is supported by the simulator"
_PCM16_WIDTH: Final = 2
_PCM16_MIN: Final = -32_768
_PCM16_MAX: Final = 32_767
_MS_PER_S: Final = 1000

type Path = Literal["caller", "agent"]
type Utterances = Mapping[int, tuple[str, str]]


class BatchAudioError(Exception):
    """Unusable audio: the ElevenLabs ``detail.status`` and its message (HTTP 400)."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True, slots=True)
class BatchAudio:
    """Decoded audio: one int16 array per channel (after any down-mix), the sample rate, and the frames."""

    channels: tuple[NDArray[np.int16], ...]
    rate: int
    frames: int

    @property
    def duration_s(self) -> float:
        return self.frames / self.rate


@dataclass(frozen=True, slots=True)
class BatchSegment:
    """What one voiced segment said: its words, which path decoded it, and the caller utterance language."""

    words: tuple[BatchWord, ...]
    path: Path
    language: str | None


def _read_wav(data: bytes) -> tuple[NDArray[np.int16], int, int]:
    """Interleaved samples, channel count, and sample rate of a 16-bit PCM WAV."""
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            width, channels, rate = reader.getsampwidth(), reader.getnchannels(), reader.getframerate()
            frames = reader.readframes(reader.getnframes())
    except (wave.Error, EOFError) as exc:
        raise BatchAudioError("invalid_audio", INVALID_AUDIO) from exc
    if width != _PCM16_WIDTH or channels < 1 or rate < MIN_SAMPLE_RATE:
        raise BatchAudioError("invalid_audio", INVALID_AUDIO)
    usable = len(frames) // (_PCM16_WIDTH * channels) * channels
    return np.frombuffer(frames, dtype="<i2")[:usable].astype(np.int16), channels, rate


def _mixdown(tracks: NDArray[np.int16]) -> NDArray[np.int16]:
    """The sample-wise clipped sum of the columns of ``tracks``."""
    mixed: NDArray[np.int16] = np.clip(tracks.astype(np.int32).sum(axis=1), _PCM16_MIN, _PCM16_MAX).astype(
        np.int16
    )
    return mixed


def read_batch_audio(data: bytes, file_format: str, *, multi_channel: bool) -> BatchAudio:
    """The channels of ``data``: raw 16 kHz mono for ``pcm_s16le_16``, else a 16-bit PCM WAV, down-mixed to
    one channel unless ``multi_channel``. Raises :class:`BatchAudioError`."""
    if file_format == "pcm_s16le_16":
        if len(data) % _PCM16_WIDTH:
            raise BatchAudioError("invalid_audio", INVALID_AUDIO)
        samples, count, rate = np.frombuffer(data, dtype="<i2").astype(np.int16), 1, PCM_S16LE_16_RATE
    else:
        samples, count, rate = _read_wav(data)
    if multi_channel and count > MAX_CHANNELS:
        msg = f"use_multi_channel supports at most {MAX_CHANNELS} channels, got {count}"
        raise BatchAudioError("too_many_channels", msg)
    tracks = samples.reshape(-1, count)
    frames = tracks.shape[0]
    if frames * _MS_PER_S < MIN_AUDIO_MS * rate:
        msg = f"The audio must be at least {MIN_AUDIO_MS} ms long"
        raise BatchAudioError("audio_too_short", msg)
    if multi_channel:
        channels = tuple(np.ascontiguousarray(tracks[:, index]) for index in range(count))
    else:
        channels = (_mixdown(tracks),)
    return BatchAudio(channels, rate, frames)


def voiced_segments(samples: NDArray[np.int16], rate: int) -> list[tuple[int, int]]:
    """``(start, end)`` sample ranges of the voiced segments: first voiced frame start to last voiced frame
    end, split by at least ``SEGMENT_GAP_MS`` of unvoiced frames, dropped under ``MIN_SPEECH_MS`` voiced."""
    frame = rate * FRAME_MS // _MS_PER_S
    whole = samples.size // frame * frame
    if whole == 0:
        return []
    frames = samples[:whole].astype(np.float64).reshape(-1, frame)
    voiced = np.flatnonzero(np.sqrt(np.mean(np.square(frames), axis=1)) >= VOICED_RMS)
    if voiced.size == 0:
        return []
    gap_frames = SEGMENT_GAP_MS // FRAME_MS
    breaks = np.flatnonzero(np.diff(voiced) - 1 >= gap_frames) + 1
    return [
        (int(group[0]) * frame, (int(group[-1]) + 1) * frame)
        for group in np.split(voiced, breaks)
        if group.size * FRAME_MS >= MIN_SPEECH_MS
    ]


def _caller_words(text: str, body_start_ms: int) -> tuple[BatchWord, ...]:
    words: list[BatchWord] = []
    for word in timed_words(text, body_start_ms):
        characters = tuple(
            (
                char,
                (word.start_ms + index * CALLER_MS_PER_CHAR) / _MS_PER_S,
                (word.start_ms + (index + 1) * CALLER_MS_PER_CHAR) / _MS_PER_S,
            )
            for index, char in enumerate(word.text)
        )
        words.append(BatchWord(word.text, word.start_ms / _MS_PER_S, word.end_ms / _MS_PER_S, characters))
    return tuple(words)


def _caller_segment(
    samples: NDArray[np.int16], rate: int, span: tuple[int, int], utterances: Utterances
) -> BatchSegment:
    """The segment resolved through its marker (40 ms pre-roll plus 260 ms window, zero-padded)."""
    start, end = span
    low = max(0, start - rate * PRE_ROLL_MS // _MS_PER_S)
    high = start + rate * MARKER_WINDOW_MS // _MS_PER_S
    window = samples[low:high]
    if window.size < high - low:
        window = np.concatenate([window, np.zeros(high - low - window.size, dtype=np.int16)])
    marker = decode_marker(window, rate)
    heard = utterances.get(marker) if marker is not None else None
    if heard is None:
        return BatchSegment((unintelligible(start / rate, end / rate),), "caller", None)
    text, language = heard
    return BatchSegment(_caller_words(text, start * _MS_PER_S // rate + MARKER_MS), "caller", language)


def transcribe_channel(
    samples: NDArray[np.int16], rate: int, *, utterances: Utterances, texts: Sequence[AgentText]
) -> list[BatchSegment]:
    """Every voiced segment of one channel, in time order."""
    segments: list[BatchSegment] = []
    for start, end in voiced_segments(samples, rate):
        slots = decode_tones(samples, rate, start, end)
        if slots is None:
            segments.append(_caller_segment(samples, rate, (start, end), utterances))
            continue
        words = match_agent_text(slots, texts, rate)
        if words is None:
            words = [unintelligible(start / rate, end / rate)]
        segments.append(BatchSegment(tuple(words), "agent", None))
    return segments


def transcribe(
    audio: BatchAudio, utterances: Utterances, generated: Sequence[str]
) -> list[list[BatchSegment]]:
    """The segments of every channel; ``utterances`` maps marker ids to ``(text, language)`` and
    ``generated`` lists the TTS generated texts (both snapshots taken on the event loop)."""
    texts = map_agent_texts(generated)
    return [
        transcribe_channel(channel, audio.rate, utterances=utterances, texts=texts)
        for channel in audio.channels
    ]
