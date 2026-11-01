"""RMS / dBFS levels and gain (specs/07-media-pipeline.md §10.2 level convention)."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from va_voice.audio import (
    apply_gain,
    dbfs_to_amplitude,
    gain_for_db,
    pcm16le_to_pcm,
    rms,
    rms_dbfs,
    ulaw_decode,
)

ASSETS = Path(__file__).resolve().parents[4] / "assets" / "audio"


def test_full_scale_square_wave_is_0_dbfs() -> None:
    square = np.array([32_767, -32_767] * 400, dtype=np.int16)
    assert rms(square) == 32_767.0
    assert rms_dbfs(square) == 0.0


@pytest.mark.parametrize("amplitude", [32_767.0, 10_000.0, 1_000.0])
def test_sine_level_is_amplitude_over_root_two(amplitude: float) -> None:
    t = np.arange(16_000) / 16_000
    sine = np.rint(amplitude * np.sin(2 * math.pi * 1000 * t)).astype(np.int16)
    expected = 20 * math.log10(amplitude / math.sqrt(2) / 32_767)
    assert rms_dbfs(sine) == pytest.approx(expected, abs=0.01)


def test_float_signals_use_full_scale_one() -> None:
    assert rms_dbfs(np.full(100, 0.5, dtype=np.float32)) == pytest.approx(20 * math.log10(0.5))


def test_large_int16_values_do_not_overflow() -> None:
    assert rms(np.full(10_000, -32_768, dtype=np.int16)) == 32_768.0


@pytest.mark.parametrize("signal", [np.zeros(160, dtype=np.int16), np.zeros(0, dtype=np.int16)])
def test_silence_and_empty_signals_are_minus_infinity(signal: NDArray[np.int16]) -> None:
    assert rms_dbfs(signal) == -math.inf


def test_gain_and_amplitude_conversions() -> None:
    assert gain_for_db(0.0) == 1.0
    assert gain_for_db(20.0) == pytest.approx(10.0)
    assert dbfs_to_amplitude(0.0) == 32_767.0
    assert dbfs_to_amplitude(-6.020_6) == pytest.approx(16_383.5, abs=0.1)


def test_apply_gain_scales_rounds_and_clips() -> None:
    x = np.array([1000, -1000, 20_000, -20_000, 3], dtype=np.int16)
    assert apply_gain(x, 2.0).tolist() == [2000, -2000, 32_767, -32_768, 6]
    assert apply_gain(x, 0.5).tolist() == [500, -500, 10_000, -10_000, 2]
    assert apply_gain(x, gain_for_db(-120)).tolist() == [0, 0, 0, 0, 0]


@pytest.mark.parametrize(("track", "target"), [("static", -40.0), ("office", -22.0)])
def test_background_assets_measure_at_their_generated_levels(track: str, target: float) -> None:
    wide = pcm16le_to_pcm((ASSETS / f"{track}.pcm16k").read_bytes())
    narrow = ulaw_decode((ASSETS / f"{track}.ulaw").read_bytes())
    assert rms_dbfs(wide) == pytest.approx(target, abs=0.5)
    assert rms_dbfs(narrow) == pytest.approx(target, abs=0.5)
