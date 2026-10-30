"""Formant caller speech synthesis (specs/30 §3.1)."""

from __future__ import annotations

import numpy as np
import pytest
from numpy.typing import NDArray

from va_sim.audio import (
    MARKER_MS,
    VOWEL_FORMANTS,
    l16be_to_pcm,
    synthesize_pcm16,
    synthesize_utterance,
    synthesize_wire,
    ulaw_decode,
    utterance_ms,
    vowel_sequence,
)

TEXT = "I'd like to reschedule my appointment to Friday afternoon."
# With one character per syllable, a run of one character gives one vowel throughout: ord mod 5 picks it.
VOWEL_CHARS = {"a": "d", "e": "e", "i": "f", "o": "g", "u": "h"}


def body(pcm: NDArray[np.int16], sample_rate: int) -> NDArray[np.float64]:
    return pcm[MARKER_MS * sample_rate // 1_000 :].astype(np.float64)


def band_share(signal: NDArray[np.float64], sample_rate: int, freq_hz: float) -> float:
    """Share of the signal's energy within ±10 % of ``freq_hz``."""
    spectrum = np.abs(np.fft.rfft(signal)) ** 2
    freqs = np.fft.rfftfreq(signal.size, 1 / sample_rate)
    band = (freqs > 0.9 * freq_hz) & (freqs < 1.1 * freq_hz)
    return float(spectrum[band].sum() / spectrum.sum())


@pytest.mark.parametrize("sample_rate", [8_000, 16_000])
@pytest.mark.parametrize("rate", [40, 70, 113])
def test_length_matches_preamble_plus_text(sample_rate: int, rate: int) -> None:
    pcm = synthesize_pcm16(1, TEXT, sample_rate=sample_rate, rate_ms_per_char=rate, seed=0)
    assert utterance_ms(TEXT, rate) == 180 + len(TEXT) * rate
    assert pcm.size == utterance_ms(TEXT, rate) * sample_rate // 1_000
    assert pcm.dtype == np.int16


def test_same_seed_same_bytes_other_seed_other_body_same_preamble() -> None:
    first = synthesize_utterance(9, TEXT, seed=1)
    preamble = MARKER_MS * 8
    assert synthesize_utterance(9, TEXT, seed=1) == first
    other = synthesize_utterance(9, TEXT, seed=2)
    assert other[:preamble] == first[:preamble]
    assert other[preamble:] != first[preamble:]


def test_other_id_other_preamble_same_body() -> None:
    first = synthesize_utterance(9, TEXT, seed=1)
    other = synthesize_utterance(10, TEXT, seed=1)
    preamble = MARKER_MS * 8
    assert other[:preamble] != first[:preamble]
    assert other[preamble:] == first[preamble:]


@pytest.mark.parametrize("sample_rate", [8_000, 16_000])
def test_peaks(sample_rate: int) -> None:
    pcm = synthesize_pcm16(3, TEXT, sample_rate=sample_rate, seed=4)
    split = MARKER_MS * sample_rate // 1_000
    assert int(np.max(np.abs(pcm[:split]))) == pytest.approx(8_231, rel=0.02)
    assert int(np.max(np.abs(pcm[split:]))) == pytest.approx(16_422, rel=0.02)


def test_ulaw_output_decodes_to_the_pcm_within_one_step() -> None:
    pcm = synthesize_pcm16(3, TEXT, seed=4).astype(np.int64)
    decoded = ulaw_decode(synthesize_utterance(3, TEXT, seed=4)).astype(np.int64)
    assert decoded.size == pcm.size
    biased = np.minimum(np.abs(pcm), 32_635) + 132
    steps = 2 ** (np.floor(np.log2(biased)).astype(np.int64) - 4)
    assert np.all(np.abs(decoded - pcm) <= steps)


def test_wire_formats() -> None:
    assert synthesize_wire(5, TEXT, sample_rate=8_000, seed=6) == synthesize_utterance(5, TEXT, seed=6)
    wide = synthesize_wire(5, TEXT, sample_rate=16_000, seed=6)
    assert l16be_to_pcm(wide).tolist() == synthesize_pcm16(5, TEXT, sample_rate=16_000, seed=6).tolist()


@pytest.mark.parametrize("sample_rate", [8_000, 16_000])
@pytest.mark.parametrize("f0_hz", [120.0, 200.0])
def test_body_is_periodic_at_f0(sample_rate: int, f0_hz: float) -> None:
    voiced = body(synthesize_pcm16(1, "d" * 14, sample_rate=sample_rate, f0_hz=f0_hz, seed=8), sample_rate)
    per_ms = sample_rate // 1_000
    window = voiced[50 * per_ms : 150 * per_ms] - voiced[50 * per_ms : 150 * per_ms].mean()
    autocorrelation = np.correlate(window, window, "full")[window.size - 1 :]
    period = sample_rate / f0_hz
    lo, hi = int(0.6 * period), int(1.4 * period)
    lag = lo + int(np.argmax(autocorrelation[lo:hi]))
    assert lag == pytest.approx(period, rel=0.05)


@pytest.mark.parametrize("sample_rate", [8_000, 16_000])
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_body_energy_is_continuous(sample_rate: int, seed: int) -> None:
    voiced = body(synthesize_pcm16(2, TEXT, sample_rate=sample_rate, seed=seed), sample_rate)
    window = 32 * sample_rate // 1_000
    count = voiced.size // window
    rms = np.sqrt(np.mean(voiced[: count * window].reshape(count, window) ** 2, axis=1))
    loud = 20 * np.log10(rms / 32_767) > -40
    assert loud.mean() >= 0.9


def test_vowel_sequence_rule_on_a_worked_example() -> None:
    # rate 90 → 2 characters per 180 ms syllable: "ab"=195→a, "cd"=199→u, "e"=101→e.
    assert vowel_sequence("abcde", 90) == ("a", "u", "e")
    # rate 70 → 2.571 characters per syllable: slices [0,2) [2,5) [5,7) of "hello!!" → 205→a, 327→i, 66→e.
    assert vowel_sequence("hello!!", 70) == ("a", "i", "e")
    # rate 400 → 0.45 characters per syllable: each syllable speaks the character under it, a=97→i, b=98→o.
    assert vowel_sequence("ab", 400) == ("i", "i", "i", "o", "o")
    assert vowel_sequence(TEXT, 70) == vowel_sequence(TEXT, 70)


@pytest.mark.parametrize("sample_rate", [8_000, 16_000])
@pytest.mark.parametrize("f0_hz", [120.0, 200.0])
def test_formants_shape_the_spectrum(sample_rate: int, f0_hz: float) -> None:
    voiced = {
        vowel: body(
            synthesize_pcm16(1, char * 8, sample_rate=sample_rate, f0_hz=f0_hz, rate_ms_per_char=180, seed=2),
            sample_rate,
        )
        for vowel, char in VOWEL_CHARS.items()
    }
    for vowel, char in VOWEL_CHARS.items():
        assert set(vowel_sequence(char * 8, 180)) == {vowel}
        for formant in VOWEL_FORMANTS[vowel][:2]:
            share = band_share(voiced[vowel], sample_rate, formant)
            for other, formants in VOWEL_FORMANTS.items():
                if all(abs(f - formant) > 0.25 * formant for f in formants):
                    assert share > band_share(voiced[other], sample_rate, formant), (vowel, formant, other)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"utterance_id": -1}, "16-bit"),
        ({"utterance_id": 65_536}, "16-bit"),
        ({"text": " \t\n"}, "non-whitespace"),
        ({"rate_ms_per_char": 0}, "rate_ms_per_char"),
        ({"f0_hz": 30.0}, "f0_hz"),
        ({"f0_hz": 401.0}, "f0_hz"),
        ({"sample_rate": 11_025}, "sample_rate"),
        ({"seed": -1}, "seed"),
    ],
)
def test_invalid_arguments(kwargs: dict[str, object], match: str) -> None:
    arguments: dict[str, object] = {"utterance_id": 1, "text": TEXT, "sample_rate": 8_000, "seed": 0}
    arguments.update(kwargs)
    with pytest.raises(ValueError, match=match):
        synthesize_pcm16(**arguments)  # type: ignore[arg-type]


def test_vowel_sequence_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="non-whitespace"):
        vowel_sequence("   ", 70)
    with pytest.raises(ValueError, match="rate_ms_per_char"):
        vowel_sequence("hi", 0)
