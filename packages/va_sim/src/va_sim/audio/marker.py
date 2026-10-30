"""Utterance marker preamble and its Goertzel decoder (specs/30-testing-and-evals.md §3.1).

The preamble is a 20 ms 1 700 Hz sync tone followed by the 16-bit utterance id, most significant bit first, as
10 ms FSK symbols (bit 0 = 1 200 Hz, bit 1 = 2 200 Hz). The phase runs on across symbol boundaries so no
clicks splatter energy between the tones. FakeElevenLabs STT decodes the marker to map caller audio back to
the registered utterance text.
"""

from __future__ import annotations

from typing import Final

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "BIT0_HZ",
    "BIT1_HZ",
    "BIT_MS",
    "MARKER_BITS",
    "MARKER_MS",
    "MAX_UTTERANCE_ID",
    "SYNC_HZ",
    "SYNC_MS",
    "check_utterance_id",
    "decode_marker",
    "goertzel_power",
    "marker_preamble",
]

SYNC_HZ: Final = 1_700
BIT0_HZ: Final = 1_200
BIT1_HZ: Final = 2_200
SYNC_MS: Final = 20
BIT_MS: Final = 10
MARKER_BITS: Final = 16
MARKER_MS: Final = SYNC_MS + MARKER_BITS * BIT_MS
MAX_UTTERANCE_ID: Final = (1 << MARKER_BITS) - 1

_MIN_SAMPLE_RATE: Final = 8_000
_DOMINANCE: Final = 0.5
_MIN_RMS: Final = 32_767 * 10 ** (-30 / 20)
_SCAN_STEP_MS: Final = 1
_BIT_TRIM_MS: Final = 1
# The first 1 ms step whose window passes the sync check can sit up to half a window (10 ms) before the true
# start (the dominance ratio equals the overlap fraction); the sync power peaks exactly at the start.
_REFINE_MS: Final = 12


def _samples(ms: int, sample_rate: int) -> int:
    return round(ms * sample_rate / 1_000)


def check_utterance_id(utterance_id: int) -> None:
    if not 0 <= utterance_id <= MAX_UTTERANCE_ID:
        msg = f"utterance_id must be within 0..{MAX_UTTERANCE_ID} (16-bit marker), got {utterance_id}"
        raise ValueError(msg)


def marker_preamble(utterance_id: int, sample_rate: int) -> NDArray[np.float64]:
    """The 180 ms preamble for ``utterance_id`` as a unit-peak float signal."""
    check_utterance_id(utterance_id)
    if sample_rate < _MIN_SAMPLE_RATE:
        msg = f"sample_rate must be at least {_MIN_SAMPLE_RATE}, got {sample_rate}"
        raise ValueError(msg)
    bits = [(utterance_id >> (MARKER_BITS - 1 - index)) & 1 for index in range(MARKER_BITS)]
    frequency = np.concatenate(
        [
            np.full(_samples(SYNC_MS, sample_rate), float(SYNC_HZ)),
            *(np.full(_samples(BIT_MS, sample_rate), float(BIT1_HZ if bit else BIT0_HZ)) for bit in bits),
        ]
    )
    phase = 2 * np.pi * np.concatenate([[0.0], np.cumsum(frequency[:-1])]) / sample_rate
    tone = np.sin(phase)
    peak = float(np.max(np.abs(tone)))
    unit: NDArray[np.float64] = tone / peak
    return unit


def goertzel_power(x: NDArray[np.float64], freq_hz: float, sample_rate: int) -> float:
    """Power of ``x`` at ``freq_hz`` (the Goertzel single-bin DFT), scaled so a tone's power is its energy.

    ``2·|Σ x[n]·e^{-jωn}|² / N``: for ``A·sin(ωn)`` over whole cycles this equals ``Σ x[n]²``, so a tone's
    share of a window is ``power / energy``.
    """
    if x.size == 0:
        return 0.0
    n = np.arange(x.size)
    projection = np.dot(x, np.exp(-2j * np.pi * freq_hz * n / sample_rate))
    return float(2 * abs(projection) ** 2 / x.size)


def _sync_candidates(signal: NDArray[np.float64], sample_rate: int) -> tuple[list[int], NDArray[np.float64]]:
    """1 ms scan offsets whose 20 ms window is sync-dominated, plus the sync power per offset."""
    window = _samples(SYNC_MS, sample_rate)
    demodulated = signal * np.exp(-2j * np.pi * SYNC_HZ * np.arange(signal.size) / sample_rate)
    running = np.concatenate([[0j], np.cumsum(demodulated)])
    running_energy = np.concatenate([[0.0], np.cumsum(signal * signal)])
    sums = running[window:] - running[:-window]
    power: NDArray[np.float64] = 2 * np.abs(sums) ** 2 / window
    energy = running_energy[window:] - running_energy[:-window]
    step = _samples(_SCAN_STEP_MS, sample_rate)
    offsets = np.arange(0, power.size, step)
    passing = (power[offsets] >= _DOMINANCE * energy[offsets]) & (energy[offsets] >= window * _MIN_RMS**2)
    return [int(offset) for offset in offsets[passing]], power


def _read_bits(signal: NDArray[np.float64], start: int, sample_rate: int) -> int | None:
    """The 16-bit id whose symbols start ``SYNC_MS`` after ``start``, or None when any symbol is not FSK."""
    trim = _samples(_BIT_TRIM_MS, sample_rate)
    symbol = _samples(BIT_MS, sample_rate)
    first = start + _samples(SYNC_MS, sample_rate)
    if first + MARKER_BITS * symbol > signal.size:
        return None
    value = 0
    for index in range(MARKER_BITS):
        lo = first + index * symbol + trim
        window = signal[lo : lo + symbol - 2 * trim]
        energy = float(np.dot(window, window))
        zero = goertzel_power(window, BIT0_HZ, sample_rate)
        one = goertzel_power(window, BIT1_HZ, sample_rate)
        if energy < window.size * _MIN_RMS**2 or zero + one < _DOMINANCE * energy:
            return None
        value = (value << 1) | int(one > zero)
    return value


def decode_marker(pcm16: NDArray[np.int16], sample_rate: int) -> int | None:
    """The first utterance id whose marker preamble occurs in ``pcm16``, or None.

    Scans in 1 ms steps for a 20 ms window where the 1 700 Hz sync holds at least half the energy (RMS at
    least -30 dBFS), moves to the sync power peak within the next 12 ms, then reads 16 symbols from 8 ms
    windows (1 ms trimmed each side). A candidate whose symbols are not 1 200/2 200 Hz-dominated is skipped.
    """
    if sample_rate < _MIN_SAMPLE_RATE:
        msg = f"sample_rate must be at least {_MIN_SAMPLE_RATE}, got {sample_rate}"
        raise ValueError(msg)
    signal = np.asarray(pcm16, dtype=np.float64)
    if signal.size < _samples(MARKER_MS, sample_rate):
        return None
    candidates, power = _sync_candidates(signal, sample_rate)
    refine = _samples(_REFINE_MS, sample_rate)
    for candidate in candidates:
        start = candidate + int(np.argmax(power[candidate : candidate + refine + 1]))
        value = _read_bits(signal, start, sample_rate)
        if value is not None:
            return value
    return None
