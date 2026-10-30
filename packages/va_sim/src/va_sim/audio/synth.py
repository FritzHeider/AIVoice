"""Synthetic caller speech: marker preamble plus a formant-synthesized voiced body (specs/30 §3.1).

The body is source-filter synthesis: a Rosenberg glottal pulse train at F0 with ±3 % period jitter, a first
difference for lip radiation, then per 180 ms syllable three parallel two-pole formant resonators for the
syllable's vowel and a raised-cosine envelope with a -12 dB floor. Vowels come deterministically from the
text and jitter from the seeded ``AudioRng``, so equal inputs give equal bytes. Synthesis is CPU-bound; async
callers run it in ``asyncio.to_thread``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Final

import numpy as np
from numpy.typing import NDArray

from va_sim.audio.codec import dbfs_peak
from va_sim.audio.marker import MARKER_MS, check_utterance_id, marker_preamble
from va_sim.audio.rng import AudioRng
from va_sim.audio.wire import NARROWBAND_RATE, check_sample_rate, encode_wire, to_int16

__all__ = [
    "VOWEL_FORMANTS",
    "synthesize_pcm16",
    "synthesize_utterance",
    "synthesize_wire",
    "utterance_ms",
    "vowel_sequence",
]

VOWEL_FORMANTS: Final[Mapping[str, tuple[float, float, float]]] = {
    "a": (730.0, 1_090.0, 2_440.0),
    "e": (530.0, 1_840.0, 2_480.0),
    "i": (270.0, 2_290.0, 3_010.0),
    "o": (570.0, 840.0, 2_410.0),
    "u": (300.0, 870.0, 2_240.0),
}
_VOWELS: Final = "aeiou"
_FORMANT_GAINS: Final = (1.0, 0.5, 0.25)
_FORMANT_BANDWIDTHS_HZ: Final = (80.0, 120.0, 160.0)
_RESONATOR_FLOOR: Final = 1e-4
_SYLLABLE_MS: Final = 180
# Raised-cosine envelope floor (-12 dB): syllables glide into each other as in connected speech instead of
# dropping to silence at every boundary, so voice activity detection sees one continuous voiced stretch.
_ENVELOPE_FLOOR: Final = 0.25
_JITTER: Final = 0.03
_OPEN_PHASE: Final = 0.40
_CLOSING_PHASE: Final = 0.16
_PREAMBLE_DBFS: Final = -12.0
_BODY_DBFS: Final = -6.0
_MIN_F0_HZ: Final = 50.0
_MAX_F0_HZ: Final = 400.0
_DEFAULT_F0_HZ: Final = 140.0
_DEFAULT_RATE_MS_PER_CHAR: Final = 70


def utterance_ms(text: str, rate_ms_per_char: int) -> int:
    """Total utterance length: the 180 ms preamble plus ``len(text) · rate_ms_per_char``."""
    return MARKER_MS + len(text) * rate_ms_per_char


def _check_text(text: str, rate_ms_per_char: int) -> None:
    if not text.strip():
        msg = "text must contain a non-whitespace character"
        raise ValueError(msg)
    if rate_ms_per_char < 1:
        msg = f"rate_ms_per_char must be at least 1, got {rate_ms_per_char}"
        raise ValueError(msg)


def vowel_sequence(text: str, rate_ms_per_char: int) -> tuple[str, ...]:
    """One vowel per 180 ms syllable: ``"aeiou"[Σ ord(chunk) mod 5]`` over the characters spoken in it.

    Syllable k covers characters ``[⌊k·c⌋, max(⌊(k+1)·c⌋, ⌊k·c⌋+1))`` (clamped to the text) with
    ``c = 180 / rate_ms_per_char`` characters per syllable.
    """
    _check_text(text, rate_ms_per_char)
    per_syllable = _SYLLABLE_MS / rate_ms_per_char
    count = math.ceil(len(text) * rate_ms_per_char / _SYLLABLE_MS)
    vowels: list[str] = []
    for k in range(count):
        lo = math.floor(k * per_syllable)
        hi = min(max(math.floor((k + 1) * per_syllable), lo + 1), len(text))
        vowels.append(_VOWELS[sum(map(ord, text[lo:hi])) % len(_VOWELS)])
    return tuple(vowels)


def _glottal_source(samples: int, f0_hz: float, sample_rate: int, rng: AudioRng) -> NDArray[np.float64]:
    """Rosenberg pulses with jittered periods, differentiated for lip radiation."""
    nominal = sample_rate / f0_hz
    count = math.ceil(samples / (nominal * (1 - _JITTER))) + 1
    periods = nominal * (1 + _JITTER * (2 * rng.uniform(count) - 1))
    starts = np.concatenate([[0.0], np.cumsum(periods)[:-1]])
    n = np.arange(samples, dtype=np.float64)
    which = np.searchsorted(starts, n, side="right") - 1
    t = n - starts[which]
    opening = _OPEN_PHASE * periods[which]
    closing = _CLOSING_PHASE * periods[which]
    rising = 0.5 * (1 - np.cos(np.pi * t / opening))
    falling = np.cos(np.pi * np.minimum(t - opening, closing) / (2 * closing))
    pulse = np.where(t < opening, rising, np.where(t < opening + closing, falling, 0.0))
    return np.diff(pulse, prepend=0.0)


def _resonator(freq_hz: float, bandwidth_hz: float, sample_rate: int) -> NDArray[np.float64]:
    """Truncated unit-peak impulse response ``rⁿ·sin(ω(n+1))`` of a two-pole resonator."""
    radius = math.exp(-math.pi * bandwidth_hz / sample_rate)
    length = math.ceil(math.log(_RESONATOR_FLOOR) / math.log(radius))
    n = np.arange(length, dtype=np.float64)
    response = radius**n * np.sin(2 * np.pi * freq_hz / sample_rate * (n + 1))
    unit: NDArray[np.float64] = response / np.max(np.abs(response))
    return unit


def _vowel_filter(source: NDArray[np.float64], vowel: str, sample_rate: int) -> NDArray[np.float64]:
    shaped = np.zeros_like(source)
    for freq, gain, bandwidth in zip(
        VOWEL_FORMANTS[vowel], _FORMANT_GAINS, _FORMANT_BANDWIDTHS_HZ, strict=True
    ):
        shaped += gain * np.convolve(source, _resonator(freq, bandwidth, sample_rate))[: source.size]
    return shaped


def _envelope(samples: int) -> NDArray[np.float64]:
    """Raised cosine from the floor to 1 and back over one syllable."""
    shaped: NDArray[np.float64] = _ENVELOPE_FLOOR + (1 - _ENVELOPE_FLOOR) * np.hanning(samples)
    return shaped


def _voiced_body(
    text: str, *, sample_rate: int, f0_hz: float, rate_ms_per_char: int, rng: AudioRng
) -> NDArray[np.float64]:
    samples = len(text) * rate_ms_per_char * sample_rate // 1_000
    syllable = _SYLLABLE_MS * sample_rate // 1_000
    source = _glottal_source(samples, f0_hz, sample_rate, rng)
    body = np.zeros(samples)
    for k, vowel in enumerate(vowel_sequence(text, rate_ms_per_char)):
        lo, hi = k * syllable, min((k + 1) * syllable, samples)
        body[lo:hi] = _vowel_filter(source[lo:hi], vowel, sample_rate) * _envelope(hi - lo)
    scaled: NDArray[np.float64] = body * (dbfs_peak(_BODY_DBFS) / np.max(np.abs(body)))
    return scaled


def synthesize_pcm16(  # noqa: PLR0913 - the §3.1 synthesis parameters, keyword-only
    utterance_id: int,
    text: str,
    *,
    sample_rate: int = NARROWBAND_RATE,
    f0_hz: float = _DEFAULT_F0_HZ,
    rate_ms_per_char: int = _DEFAULT_RATE_MS_PER_CHAR,
    seed: int,
) -> NDArray[np.int16]:
    """PCM16 samples at ``sample_rate``: the marker preamble (-12 dBFS), then the voiced body (-6 dBFS)."""
    check_utterance_id(utterance_id)
    _check_text(text, rate_ms_per_char)
    check_sample_rate(sample_rate)
    if not _MIN_F0_HZ <= f0_hz <= _MAX_F0_HZ:
        msg = f"f0_hz must be within {_MIN_F0_HZ:g}..{_MAX_F0_HZ:g}, got {f0_hz}"
        raise ValueError(msg)
    preamble = marker_preamble(utterance_id, sample_rate) * dbfs_peak(_PREAMBLE_DBFS)
    body = _voiced_body(
        text, sample_rate=sample_rate, f0_hz=f0_hz, rate_ms_per_char=rate_ms_per_char, rng=AudioRng(seed)
    )
    return to_int16(np.concatenate([preamble, body]))


def synthesize_utterance(
    utterance_id: int,
    text: str,
    *,
    f0_hz: float = _DEFAULT_F0_HZ,
    rate_ms_per_char: int = _DEFAULT_RATE_MS_PER_CHAR,
    seed: int,
) -> bytes:
    """μ-law 8 kHz bytes of the utterance (the §3.1 signature)."""
    pcm = synthesize_pcm16(utterance_id, text, f0_hz=f0_hz, rate_ms_per_char=rate_ms_per_char, seed=seed)
    return encode_wire(pcm, NARROWBAND_RATE)


def synthesize_wire(  # noqa: PLR0913 - the §3.1 synthesis parameters, keyword-only
    utterance_id: int,
    text: str,
    *,
    sample_rate: int,
    seed: int,
    f0_hz: float = _DEFAULT_F0_HZ,
    rate_ms_per_char: int = _DEFAULT_RATE_MS_PER_CHAR,
) -> bytes:
    """Wire bytes: μ-law at 8 kHz, or L16 big-endian synthesized natively at 16 kHz (hd_audio)."""
    pcm = synthesize_pcm16(
        utterance_id, text, sample_rate=sample_rate, f0_hz=f0_hz, rate_ms_per_char=rate_ms_per_char, seed=seed
    )
    return encode_wire(pcm, sample_rate)
