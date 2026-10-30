"""In-band DTMF synthesis (specs/30 §3.1)."""

from __future__ import annotations

import numpy as np
import pytest

from va_sim.audio import DTMF_FREQS, goertzel_power, l16be_to_pcm, synthesize_dtmf, ulaw_decode

ALL_FREQS = sorted({freq for pair in DTMF_FREQS.values() for freq in pair})


def test_keypad_has_sixteen_digits() -> None:
    assert sorted(DTMF_FREQS) == sorted("0123456789*#ABCD")
    assert DTMF_FREQS["1"] == (697, 1_209)
    assert DTMF_FREQS["0"] == (941, 1_336)
    assert DTMF_FREQS["D"] == (941, 1_633)


@pytest.mark.parametrize("digit", sorted(DTMF_FREQS))
@pytest.mark.parametrize("sample_rate", [8_000, 16_000])
def test_each_digit_carries_its_row_and_column(digit: str, sample_rate: int) -> None:
    wire = synthesize_dtmf(digit, sample_rate=sample_rate)
    pcm = ulaw_decode(wire) if sample_rate == 8_000 else l16be_to_pcm(wire)
    signal = pcm.astype(np.float64)
    powers = {freq: goertzel_power(signal, freq, sample_rate) for freq in ALL_FREQS}
    row, column = DTMF_FREQS[digit]
    strongest_other = max(power for freq, power in powers.items() if freq not in (row, column))
    assert powers[row] >= 10 * strongest_other
    assert powers[column] >= 10 * strongest_other


def test_lengths_and_level() -> None:
    narrow = synthesize_dtmf("5")
    wide = synthesize_dtmf("5", sample_rate=16_000)
    assert len(narrow) == 960
    assert len(wide) == 3_840
    peak = int(np.max(np.abs(l16be_to_pcm(wide).astype(np.int32))))
    assert 20 * np.log10(peak / 32_767) == pytest.approx(-4.0, abs=0.3)
    assert len(synthesize_dtmf("#", 40)) == 320


@pytest.mark.parametrize(
    ("digit", "ms", "sample_rate"), [("E", 120, 8_000), ("12", 120, 8_000), ("1", 0, 8_000)]
)
def test_invalid_arguments(digit: str, ms: int, sample_rate: int) -> None:
    with pytest.raises(ValueError, match=r"digit|ms"):
        synthesize_dtmf(digit, ms, sample_rate=sample_rate)


def test_unsupported_sample_rate() -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        synthesize_dtmf("1", sample_rate=44_100)
