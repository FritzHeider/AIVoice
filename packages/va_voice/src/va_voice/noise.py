"""``noise_cancellation`` on the VAD path: a streaming spectral gate (specs/07-media-pipeline.md §10.1).

The STT path is never filtered (Scribe gets ``filter_background_audio=true`` instead); this gate only
conditions the 16 kHz PCM that feeds Silero VAD. STFT: 512-point periodic Hann window, hop 256. The periodic
Hann sums to exactly 1 at 50 % overlap, so plain overlap-add of the inverse transforms reconstructs the input
when every gain is 1. The noise profile is the per-bin 20th percentile of the magnitudes of the trailing 1.5 s
of frames seen while VAD reports non-speech (:meth:`SpectralGate.set_speech`), scaled from the 20th percentile
to the mean of a Rayleigh distribution (stationary noise magnitudes); per-bin gains ``max(0.25, 1 - 1.5 x
noise / mag)`` are smoothed over time with alpha = 0.6.

Output is the input delayed by exactly 256 samples (16 ms) of stream time: :meth:`SpectralGate.process`
returns whole hops (a multiple of 256 samples over the stream) as soon as they are final, so VAD event times
on the gated stream are :data:`LATENCY_MS` later than on the raw one.
"""

from __future__ import annotations

import math
from typing import Final

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "FFT_SIZE",
    "GAIN_FLOOR",
    "HOP",
    "LATENCY_MS",
    "LATENCY_SAMPLES",
    "MIN_PROFILE_FRAMES",
    "NOISE_PERCENTILE",
    "OVER_SUBTRACTION",
    "PERCENTILE_TO_MEAN",
    "PROFILE_FRAMES",
    "SMOOTHING",
    "SpectralGate",
]

SAMPLE_RATE: Final = 16_000
FFT_SIZE: Final = 512
HOP: Final = 256
GAIN_FLOOR: Final = 0.25
"""-12 dB."""
OVER_SUBTRACTION: Final = 1.5
NOISE_PERCENTILE: Final = 20
SMOOTHING: Final = 0.6
"""``g = alpha x g_prev + (1 - alpha) x g_raw``."""
PROFILE_FRAMES: Final = math.ceil(1.5 * SAMPLE_RATE / HOP)
"""Non-speech frames in the trailing 1.5 s profile window (94)."""
MIN_PROFILE_FRAMES: Final = 16
"""Gains stay at 1 until this many non-speech frames (≈ 0.25 s) were seen."""
LATENCY_SAMPLES: Final = HOP
LATENCY_MS: Final = LATENCY_SAMPLES * 1000 // SAMPLE_RATE
PERCENTILE_TO_MEAN: Final = math.sqrt(math.pi / 2) / math.sqrt(-2 * math.log(1 - NOISE_PERCENTILE / 100))
"""Rayleigh mean / 20th percentile (≈ 1.876)."""

_BINS: Final = FFT_SIZE // 2 + 1
_EPSILON: Final = 1e-9
_INT16_MIN: Final = -32_768
_INT16_MAX: Final = 32_767
_WINDOW: Final = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(FFT_SIZE) / FFT_SIZE)).astype(np.float64)


class SpectralGate:
    """Streaming noise gate for one 16 kHz leg; feed VAD decisions back through :meth:`set_speech`."""

    __slots__ = (
        "_dirty",
        "_gain",
        "_input",
        "_noise",
        "_overlap",
        "_ring",
        "_ring_count",
        "_ring_next",
        "_speech",
    )

    def __init__(self) -> None:
        self._ring = np.zeros((PROFILE_FRAMES, _BINS), dtype=np.float64)
        self._noise = np.zeros(_BINS, dtype=np.float64)
        self._input = np.zeros(LATENCY_SAMPLES, dtype=np.float64)
        self._overlap = np.zeros(FFT_SIZE, dtype=np.float64)
        self._gain = np.ones(_BINS, dtype=np.float64)
        self._ring_count = 0
        self._ring_next = 0
        self._dirty = False
        self._speech = False

    @property
    def profile_ready(self) -> bool:
        """Enough non-speech was seen to start gating."""
        return self._ring_count >= MIN_PROFILE_FRAMES

    @property
    def noise_profile(self) -> NDArray[np.float64]:
        """The current per-bin noise magnitude estimate (a copy; zeros before :attr:`profile_ready`)."""
        self._refresh_profile()
        return self._noise.copy()

    def set_speech(self, active: bool) -> None:
        """VAD feedback: the profile learns only while ``active`` is false."""
        self._speech = active

    def reset(self) -> None:
        """Forget the profile, the smoothing state and buffered audio (a new leg)."""
        self._ring.fill(0.0)
        self._noise.fill(0.0)
        self._input = np.zeros(LATENCY_SAMPLES, dtype=np.float64)
        self._overlap.fill(0.0)
        self._gain.fill(1.0)
        self._ring_count = 0
        self._ring_next = 0
        self._dirty = False
        self._speech = False

    def process(self, pcm16k: NDArray[np.int16]) -> NDArray[np.int16]:
        """Gate ``pcm16k`` (any length); returns the final samples so far, delayed by 256 samples."""
        if pcm16k.dtype != np.int16 or pcm16k.ndim != 1:
            message = f"expected 1-D int16 samples, got {pcm16k.dtype} with shape {pcm16k.shape}"
            raise ValueError(message)
        pending = np.concatenate([self._input, pcm16k.astype(np.float64)])
        hops = max(0, (pending.size - FFT_SIZE) // HOP + 1)
        out = np.empty(hops * HOP, dtype=np.float64)
        for index in range(hops):
            start = index * HOP
            self._overlap += self._frame(pending[start : start + FFT_SIZE])
            out[start : start + HOP] = self._overlap[:HOP]
            self._overlap[:-HOP] = self._overlap[HOP:]
            self._overlap[-HOP:] = 0.0
        self._input = pending[hops * HOP :]
        return np.clip(np.rint(out), _INT16_MIN, _INT16_MAX).astype(np.int16)

    def _frame(self, frame: NDArray[np.float64]) -> NDArray[np.float64]:
        spectrum = np.fft.rfft(frame * _WINDOW)
        magnitude = np.abs(spectrum)
        if not self._speech:
            self._ring[self._ring_next] = magnitude
            self._ring_next = (self._ring_next + 1) % PROFILE_FRAMES
            self._ring_count = min(self._ring_count + 1, PROFILE_FRAMES)
            self._dirty = True
        if not self.profile_ready:
            return np.fft.irfft(spectrum, n=FFT_SIZE)
        self._refresh_profile()
        raw = np.maximum(GAIN_FLOOR, 1.0 - OVER_SUBTRACTION * self._noise / np.maximum(magnitude, _EPSILON))
        self._gain = SMOOTHING * self._gain + (1.0 - SMOOTHING) * raw
        return np.fft.irfft(spectrum * self._gain, n=FFT_SIZE)

    def _refresh_profile(self) -> None:
        if not self._dirty or not self.profile_ready:
            return
        history = self._ring[: self._ring_count]
        self._noise = PERCENTILE_TO_MEAN * np.percentile(history, NOISE_PERCENTILE, axis=0)
        self._dirty = False
