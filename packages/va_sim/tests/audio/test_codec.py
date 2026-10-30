"""Simulator G.711 μ-law codec and L16 helpers (specs/07 codec tables)."""

from __future__ import annotations

import numpy as np
import pytest

from va_sim.audio import (
    PCM16_TO_ULAW,
    ULAW_SILENCE,
    ULAW_TO_PCM16,
    dbfs_peak,
    l16be_to_pcm,
    pcm_to_l16be,
    ulaw_decode,
    ulaw_encode,
)

ALL_SAMPLES = np.arange(-32_768, 32_768, dtype=np.int32).astype(np.int16)


def g711_decode(code: int) -> int:
    """ITU-T G.711 μ-law expansion written out per code (independent of the table construction)."""
    value = ~code & 0xFF
    sign, exponent, mantissa = value >> 7, (value >> 4) & 0x7, value & 0xF
    magnitude = ((2 * mantissa + 33) << (exponent + 2)) - 132
    return -magnitude if sign else magnitude


def segment_step(sample: int) -> int:
    """The quantization step of the μ-law segment holding ``sample``."""
    biased = min(abs(sample), 32_635) + 132
    return 1 << (biased.bit_length() - 5)


@pytest.mark.parametrize(
    ("sample", "code"), [(0, 0xFF), (32_767, 0x80), (-32_768, 0x00), (-1, 0x7F), (32_635, 0x80)]
)
def test_encode_known_vectors(sample: int, code: int) -> None:
    assert ulaw_encode(np.array([sample], dtype=np.int16)) == bytes([code])


@pytest.mark.parametrize(("code", "sample"), [(0xFF, 0), (0x7F, 0), (0x80, 32_124), (0x00, -32_124)])
def test_decode_known_vectors(code: int, sample: int) -> None:
    assert ulaw_decode(bytes([code])).tolist() == [sample]


def test_decode_table_matches_the_g711_formula() -> None:
    assert ULAW_TO_PCM16.shape == (256,)
    assert ULAW_TO_PCM16.dtype == np.int16
    assert ULAW_TO_PCM16.tolist() == [g711_decode(code) for code in range(256)]


def test_encode_table_shape_and_silence() -> None:
    assert PCM16_TO_ULAW.shape == (65_536,)
    assert PCM16_TO_ULAW.dtype == np.uint8
    assert PCM16_TO_ULAW[32_768] == ULAW_SILENCE


def test_full_range_round_trip_stays_within_one_segment_step() -> None:
    decoded = ulaw_decode(ulaw_encode(ALL_SAMPLES)).astype(np.int64)
    clipped = np.clip(ALL_SAMPLES.astype(np.int64), -32_635, 32_635)
    steps = np.array([segment_step(int(sample)) for sample in ALL_SAMPLES])
    assert np.all(np.abs(decoded - clipped) < steps)


def test_every_code_is_a_fixed_point_of_decode_then_encode() -> None:
    codes = bytes(range(256))
    reencoded = ulaw_encode(ulaw_decode(codes))
    zero_codes = {0x7F, 0xFF}
    assert [c for c, r in zip(codes, reencoded, strict=True) if c != r and c not in zero_codes] == []


def test_encode_clips_beyond_32635() -> None:
    loud = np.array([32_635, 32_700, 32_767, -32_635, -32_700, -32_768], dtype=np.int16)
    assert ulaw_encode(loud) == bytes([0x80] * 3 + [0x00] * 3)


def test_l16_big_endian_round_trip_and_byte_order() -> None:
    assert pcm_to_l16be(np.array([0x0102, -2], dtype=np.int16)) == b"\x01\x02\xff\xfe"
    assert l16be_to_pcm(b"\x01\x02\xff\xfe").tolist() == [0x0102, -2]
    assert l16be_to_pcm(pcm_to_l16be(ALL_SAMPLES)).tolist() == ALL_SAMPLES.tolist()


def test_dbfs_peak() -> None:
    assert (dbfs_peak(0), dbfs_peak(-6), dbfs_peak(-12)) == (32_767, 16_422, 8_231)
