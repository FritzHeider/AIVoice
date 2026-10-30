"""WAV files as caller audio (specs/30-testing-and-evals.md §3.5 ``greeting_audio``, Decision 2026-10-29
P03-T03c rule 4).

Only 16-bit PCM WAV is read; channels are averaged to mono and the signal is linearly resampled to the stream
rate. Reading is blocking file I/O plus CPU work: async callers run it in a worker thread.
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Final

import numpy as np
from numpy.typing import NDArray

from va_sim.audio.wire import to_int16

__all__ = ["MAX_WAV_MS", "read_wav_pcm"]

MAX_WAV_MS: Final = 60_000
_PCM16_WIDTH: Final = 2


def _resample(samples: NDArray[np.float64], rate: int, sample_rate: int) -> NDArray[np.float64]:
    if rate == sample_rate:
        return samples
    count = round(samples.size * sample_rate / rate)
    positions = np.arange(count, dtype=np.float64) * rate / sample_rate
    resampled: NDArray[np.float64] = np.interp(positions, np.arange(samples.size, dtype=np.float64), samples)
    return resampled


def read_wav_pcm(path: Path, sample_rate: int, *, max_ms: int = MAX_WAV_MS) -> NDArray[np.int16]:
    """Mono PCM16 samples of ``path`` at ``sample_rate``.

    Raises ``ValueError`` (with the file name) for anything but a non-empty 16-bit PCM WAV of at most
    ``max_ms``; ``OSError`` (a missing file) passes through.
    """
    try:
        with wave.open(str(path), "rb") as reader:
            width, channels, rate = reader.getsampwidth(), reader.getnchannels(), reader.getframerate()
            frames = reader.getnframes()
            if width != _PCM16_WIDTH:
                msg = f"{path.name}: only 16-bit PCM WAV is supported, got {8 * width}-bit samples"
                raise ValueError(msg)
            if frames == 0:
                msg = f"{path.name}: the WAV file has no audio"
                raise ValueError(msg)
            if frames * 1000 > max_ms * rate:
                msg = f"{path.name}: the WAV file is longer than {max_ms} ms"
                raise ValueError(msg)
            data = reader.readframes(frames)
    except (wave.Error, EOFError) as exc:
        msg = f"{path.name}: not a PCM WAV file ({exc})"
        raise ValueError(msg) from exc
    usable = len(data) // (_PCM16_WIDTH * channels) * channels
    samples = np.frombuffer(data, dtype="<i2")[:usable].astype(np.float64).reshape(-1, channels).mean(axis=1)
    return to_int16(_resample(samples, rate, sample_rate))
