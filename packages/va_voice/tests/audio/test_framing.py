"""Re-framing arbitrary chunks into exact 20 ms frames (specs/07-media-pipeline.md §4 Framer)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from va_voice.audio import L16_16000, PCM16LE_16000, ULAW_8000, AudioFormat, Framer, join_frames, split_frames


def test_exact_frames_pass_straight_through() -> None:
    framer = Framer(ULAW_8000)
    frame = bytes(range(160))
    assert framer.feed(frame) == [frame]
    assert framer.pending == 0
    assert framer.flush() is None


def test_small_chunks_are_buffered_until_a_frame_is_complete() -> None:
    framer = Framer(ULAW_8000)
    data = bytes(i % 251 for i in range(400))
    assert framer.feed(data[:100]) == []
    assert framer.pending == 100
    assert framer.feed(data[100:330]) == [data[:160], data[160:320]]
    assert framer.pending == 10
    assert framer.feed(data[330:]) == []
    assert framer.pending == 80


def test_flush_pads_ulaw_with_0xff() -> None:
    framer = Framer(ULAW_8000)
    framer.feed(b"\x01" * 50)
    assert framer.flush() == b"\x01" * 50 + b"\xff" * 110
    assert framer.pending == 0
    assert framer.flush() is None


@pytest.mark.parametrize("fmt", [L16_16000, PCM16LE_16000])
def test_flush_pads_16_bit_formats_with_zeros(fmt: AudioFormat) -> None:
    framer = Framer(fmt)
    framer.feed(b"\x7f" * 101)
    assert framer.flush() == b"\x7f" * 101 + b"\x00" * 539


def test_dangling_odd_byte_is_kept_for_the_next_frame() -> None:
    framer = Framer(L16_16000)
    data = bytes(i % 256 for i in range(640))
    assert framer.feed(data[:321]) == []
    assert framer.pending == 321
    assert framer.feed(data[321:]) == [data]


def test_feeding_after_flush_starts_a_fresh_frame() -> None:
    framer = Framer(ULAW_8000)
    framer.feed(b"\x00" * 30)
    framer.flush()
    frame = bytes(range(160))
    assert framer.feed(frame) == [frame]


def test_reset_drops_buffered_bytes() -> None:
    framer = Framer(ULAW_8000)
    framer.feed(b"\x00" * 30)
    framer.reset()
    assert framer.pending == 0
    assert framer.flush() is None
    assert framer.format is ULAW_8000


@given(
    st.binary(max_size=3000),
    st.lists(st.integers(1, 700), max_size=30),
    st.sampled_from([ULAW_8000, L16_16000]),
)
def test_any_chunking_yields_whole_frames_that_reassemble_the_input(
    data: bytes, cuts: list[int], fmt: AudioFormat
) -> None:
    framer = Framer(fmt)
    frames: list[bytes] = []
    start = 0
    for size in [*cuts, len(data)]:
        frames += framer.feed(data[start : start + size])
        start += size
    assert all(len(f) == fmt.frame_bytes for f in frames)
    remainder = framer.pending
    tail = framer.flush()
    assert len(frames) == len(data) // fmt.frame_bytes
    if remainder:
        assert tail is not None
        assert len(tail) == fmt.frame_bytes
        frames.append(tail[:remainder])
    else:
        assert tail is None
    assert join_frames(frames) == data


@given(st.binary(max_size=2000))
def test_split_and_join_are_inverse(data: bytes) -> None:
    frames, rest = split_frames(data, ULAW_8000)
    assert all(len(f) == 160 for f in frames)
    assert len(rest) < 160
    assert join_frames([*frames, rest]) == data


def test_five_frames_join_into_one_100_ms_scribe_chunk() -> None:
    frames, rest = split_frames(bytes(800), ULAW_8000)
    assert rest == b""
    assert len(join_frames(frames[:5])) == 800
    assert ULAW_8000.duration_ms(len(join_frames(frames))) == 100.0
