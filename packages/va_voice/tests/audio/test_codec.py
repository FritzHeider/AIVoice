"""G.711 μ-law tables and PCM conversions (specs/07-media-pipeline.md §4 unit tests)."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.extra.numpy import arrays
from numpy.typing import NDArray

from va_sim.audio import codec as sim_codec
from va_voice.audio import (
    PCM16_TO_ULAW,
    ULAW_TO_PCM16,
    L16ByteOrder,
    float32_to_pcm,
    l16_to_pcm,
    l16be_to_pcm,
    pcm16le_to_pcm,
    pcm_to_float32,
    pcm_to_l16,
    pcm_to_l16be,
    pcm_to_pcm16le,
    ulaw_decode,
    ulaw_encode,
)

ALL_SAMPLES = np.arange(-32_768, 32_768, dtype=np.int32).astype(np.int16)


def g711_decode(code: int) -> int:
    """Reference decode written straight from the G.711 description, one code at a time."""
    u = ~code & 0xFF
    exponent = (u >> 4) & 7
    mantissa = u & 0xF
    magnitude = (((mantissa << 3) + 0x84) << exponent) - 0x84
    return -magnitude if u & 0x80 else magnitude


def segment_step(sample: int) -> int:
    """The quantization step of the μ-law segment holding ``|sample|`` (clipped)."""
    biased = min(abs(sample), 32_635) + 0x84
    return 1 << (biased.bit_length() - 8 + 3)


# (sample, code, decoded): G.711 encode of ``sample`` and the reconstruction value of that code.
GOLDEN: list[tuple[int, int, int]] = [
    (0, 0xFF, 0),
    (32_767, 0x80, 32_124),
    (-32_768, 0x00, -32_124),
    (32_635, 0x80, 32_124),
    (-32_635, 0x00, -32_124),
    (1, 0xFF, 0),
    (-1, 0x7F, 0),
    (8, 0xFE, 8),
    (-8, 0x7E, -8),
    (100, 0xF2, 104),
    (-100, 0x72, -104),
    (120, 0xF0, 120),
    (1000, 0xCE, 988),
    (-1000, 0x4E, -988),
    (8000, 0xA0, 7_932),
]


@pytest.mark.parametrize(("sample", "code", "decoded"), GOLDEN)
def test_golden_vectors(sample: int, code: int, decoded: int) -> None:
    wire = ulaw_encode(np.array([sample], dtype=np.int16))
    assert (wire, ulaw_decode(wire).tolist()) == (bytes([code]), [decoded])


def test_tables_have_spec_shapes_and_dtypes() -> None:
    assert ULAW_TO_PCM16.shape == (256,)
    assert ULAW_TO_PCM16.dtype == np.int16
    assert PCM16_TO_ULAW.shape == (65_536,)
    assert PCM16_TO_ULAW.dtype == np.uint8


def test_decode_table_matches_the_g711_formula_for_every_code() -> None:
    assert ULAW_TO_PCM16.tolist() == [g711_decode(c) for c in range(256)]


def test_round_trip_error_is_within_one_quantization_step_for_all_65536_inputs() -> None:
    decoded = ulaw_decode(ulaw_encode(ALL_SAMPLES)).astype(np.int32)
    clipped = np.clip(ALL_SAMPLES.astype(np.int32), -32_635, 32_635)
    steps = np.array([segment_step(int(s)) for s in ALL_SAMPLES], dtype=np.int32)
    assert bool(np.all(np.abs(decoded - clipped) <= steps))


def test_decoding_then_encoding_reproduces_every_code_except_negative_zero() -> None:
    codes = bytes(range(256))
    again = ulaw_encode(ulaw_decode(codes))
    assert [c for c in range(256) if again[c] != c] == [0x7F]
    assert again[0x7F] == 0xFF


def test_encode_clips_magnitudes_beyond_32635() -> None:
    loud = np.array([32_635, 32_700, 32_767, -32_635, -32_700, -32_768], dtype=np.int16)
    assert ulaw_encode(loud) == bytes([0x80, 0x80, 0x80, 0x00, 0x00, 0x00])


def test_decoded_value_is_monotonic_in_the_sample() -> None:
    decoded = ulaw_decode(ulaw_encode(ALL_SAMPLES))
    assert bool(np.all(np.diff(decoded.astype(np.int32)) >= 0))


def test_tables_are_bit_identical_with_the_simulator_codec() -> None:
    assert np.array_equal(ULAW_TO_PCM16, sim_codec.ULAW_TO_PCM16)
    assert np.array_equal(PCM16_TO_ULAW, sim_codec.PCM16_TO_ULAW)


@given(arrays(np.int16, st.integers(0, 400)))
def test_quantization_is_idempotent_after_one_pass(x: NDArray[np.int16]) -> None:
    once = ulaw_decode(ulaw_encode(x))
    assert np.array_equal(ulaw_decode(ulaw_encode(once)), once)


def test_l16_wire_is_big_endian() -> None:
    x = np.array([1, -2, 0x1234], dtype=np.int16)
    assert pcm_to_l16be(x) == b"\x00\x01\xff\xfe\x12\x34"
    assert l16be_to_pcm(b"\x00\x01\xff\xfe\x12\x34").tolist() == [1, -2, 0x1234]


def test_pcm16le_is_little_endian() -> None:
    x = np.array([1, -2, 0x1234], dtype=np.int16)
    assert pcm_to_pcm16le(x) == b"\x01\x00\xfe\xff\x34\x12"
    assert pcm16le_to_pcm(b"\x01\x00\xfe\xff\x34\x12").tolist() == [1, -2, 0x1234]


@pytest.mark.parametrize("byte_order", ["big", "little"])
def test_l16_round_trips_every_sample_in_both_byte_orders(byte_order: L16ByteOrder) -> None:
    wire = pcm_to_l16(ALL_SAMPLES, byte_order)
    assert len(wire) == 2 * ALL_SAMPLES.size
    back = l16_to_pcm(wire, byte_order)
    assert back.dtype == np.int16
    assert np.array_equal(back, ALL_SAMPLES)


def test_byte_orders_are_swapped_views_of_each_other() -> None:
    x = np.array([0x0102, -0x0304], dtype=np.int16)
    big = pcm_to_l16(x, "big")
    little = pcm_to_l16(x, "little")
    assert big == bytes(b for pair in zip(little[1::2], little[0::2], strict=True) for b in pair)


@pytest.mark.parametrize("convert", [l16be_to_pcm, pcm16le_to_pcm])
def test_odd_byte_counts_are_rejected(convert: Callable[[bytes], NDArray[np.int16]]) -> None:
    with pytest.raises(ValueError, match="whole samples; got 3 bytes"):
        convert(b"\x00\x01\x02")


def test_float32_conversion_scales_by_32768() -> None:
    x = np.array([-32_768, 0, 16_384, 32_767], dtype=np.int16)
    f = pcm_to_float32(x)
    assert f.dtype == np.float32
    assert f.tolist() == [-1.0, 0.0, 0.5, 32_767 / 32_768]


def test_float32_round_trip_is_exact_for_every_sample() -> None:
    assert np.array_equal(float32_to_pcm(pcm_to_float32(ALL_SAMPLES)), ALL_SAMPLES)


def test_float32_to_pcm_clips_out_of_range_values() -> None:
    f = np.array([1.5, 1.0, -1.0, -2.0, 0.25], dtype=np.float32)
    assert float32_to_pcm(f).tolist() == [32_767, 32_767, -32_768, -32_768, 8_192]
