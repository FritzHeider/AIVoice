"""Utterance marker preamble and decoder (specs/30 §3.1)."""

from __future__ import annotations

import random

import numpy as np
import pytest
from numpy.typing import NDArray

from va_sim.audio import (
    MARKER_MS,
    AudioRng,
    decode_marker,
    goertzel_power,
    marker_preamble,
    synthesize_pcm16,
    synthesize_utterance,
    ulaw_decode,
)
from va_sim.audio.marker import BIT0_HZ, BIT1_HZ, SYNC_HZ

TEXT = "Hi, I'm calling about my order from last Tuesday."
_IDS_RNG = random.Random(7)  # noqa: S311 - test data, not cryptography
IDS = sorted({0, 65_535, *(_IDS_RNG.randrange(65_536) for _ in range(20))})


def upsample_to_16k(pcm8k: NDArray[np.int16]) -> NDArray[np.int16]:
    positions = np.arange(pcm8k.size * 2) / 2
    return np.rint(np.interp(positions, np.arange(pcm8k.size), pcm8k)).astype(np.int16)


def noise(n: int, seed: int, dbfs: float) -> NDArray[np.float64]:
    """Uniform white noise with RMS at ``dbfs``."""
    uniform = AudioRng(seed).uniform(n)
    scaled: NDArray[np.float64] = (2 * uniform - 1) * np.sqrt(3) * 32_767 * 10 ** (dbfs / 20)
    return scaled


def narrowband(utterance_id: int) -> NDArray[np.int16]:
    return ulaw_decode(synthesize_utterance(utterance_id, TEXT, seed=utterance_id + 11))


@pytest.mark.parametrize("utterance_id", IDS)
def test_marker_survives_ulaw_round_trip(utterance_id: int) -> None:
    assert decode_marker(narrowband(utterance_id), 8_000) == utterance_id


@pytest.mark.parametrize("utterance_id", IDS)
def test_marker_survives_resampling_to_16k(utterance_id: int) -> None:
    assert decode_marker(upsample_to_16k(narrowband(utterance_id)), 16_000) == utterance_id


@pytest.mark.parametrize("utterance_id", IDS)
def test_marker_decodes_from_native_16k_synthesis(utterance_id: int) -> None:
    pcm = synthesize_pcm16(utterance_id, TEXT, sample_rate=16_000, seed=3)
    assert decode_marker(pcm, 16_000) == utterance_id


@pytest.mark.parametrize("utterance_id", IDS)
def test_marker_decodes_behind_leading_silence_and_noise(utterance_id: int) -> None:
    lead = random.Random(utterance_id).randrange(0, 4_001)  # noqa: S311 - test data
    signal = np.concatenate([np.zeros(lead), narrowband(utterance_id).astype(np.float64)])
    noisy = np.rint(signal + noise(signal.size, utterance_id, -40.0)).astype(np.int16)
    assert decode_marker(noisy, 8_000) == utterance_id


def test_no_marker_in_silence_noise_body_or_truncated_preamble() -> None:
    pcm = synthesize_pcm16(4_242, TEXT, seed=5)
    preamble = MARKER_MS * 8
    assert decode_marker(np.zeros(8_000, dtype=np.int16), 8_000) is None
    assert decode_marker(np.rint(noise(8_000, 1, -20.0)).astype(np.int16), 8_000) is None
    assert decode_marker(pcm[preamble:], 8_000) is None
    assert decode_marker(pcm[: preamble - 400], 8_000) is None
    assert decode_marker(np.concatenate([pcm[: preamble - 400], np.zeros(4_000, np.int16)]), 8_000) is None
    assert decode_marker(pcm[:100], 8_000) is None


def test_decoder_skips_a_false_sync_and_finds_the_marker_after_it() -> None:
    t = np.arange(400) / 8_000
    false_sync = np.rint(8_000 * np.sin(2 * np.pi * SYNC_HZ * t)).astype(np.int16)
    gap = np.zeros(800, dtype=np.int16)
    pcm = np.concatenate([false_sync, gap, synthesize_pcm16(77, TEXT, seed=1)])
    assert decode_marker(pcm, 8_000) == 77


@pytest.mark.parametrize("sample_rate", [8_000, 16_000])
def test_preamble_layout(sample_rate: int) -> None:
    preamble = marker_preamble(0b1000_0000_0000_0001, sample_rate)
    per_ms = sample_rate // 1_000
    assert preamble.size == MARKER_MS * per_ms
    assert float(np.max(np.abs(preamble))) == pytest.approx(1.0)
    sync = preamble[: 20 * per_ms]
    first_bit = preamble[20 * per_ms : 30 * per_ms]
    second_bit = preamble[30 * per_ms : 40 * per_ms]
    assert goertzel_power(sync, SYNC_HZ, sample_rate) > 10 * max(
        goertzel_power(sync, BIT0_HZ, sample_rate), goertzel_power(sync, BIT1_HZ, sample_rate)
    )
    assert goertzel_power(first_bit, BIT1_HZ, sample_rate) > 10 * goertzel_power(
        first_bit, BIT0_HZ, sample_rate
    )
    assert goertzel_power(second_bit, BIT0_HZ, sample_rate) > 10 * goertzel_power(
        second_bit, BIT1_HZ, sample_rate
    )


def test_preamble_is_phase_continuous() -> None:
    preamble = marker_preamble(0b0101_0101_0101_0101, 8_000)
    max_step = 2 * np.sin(np.pi * BIT1_HZ / 8_000)
    assert float(np.max(np.abs(np.diff(preamble)))) <= max_step + 1e-9


def test_goertzel_power_of_a_tone_equals_its_energy() -> None:
    tone = 0.5 * np.sin(2 * np.pi * 1_000 * np.arange(160) / 8_000)
    assert goertzel_power(tone, 1_000, 8_000) == pytest.approx(float(np.dot(tone, tone)))
    assert goertzel_power(np.zeros(0), 1_000, 8_000) == 0.0


@pytest.mark.parametrize("utterance_id", [-1, 65_536])
def test_preamble_rejects_ids_outside_16_bits(utterance_id: int) -> None:
    with pytest.raises(ValueError, match="16-bit"):
        marker_preamble(utterance_id, 8_000)


def test_low_sample_rates_are_rejected() -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        marker_preamble(1, 4_000)
    with pytest.raises(ValueError, match="sample_rate"):
        decode_marker(np.zeros(4_000, dtype=np.int16), 4_000)
