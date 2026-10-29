"""pcm16_wav: interleaving, zero padding, validation."""

from __future__ import annotations

import io
import struct
import wave

import pytest

from va_sim.wav import pcm16_wav


def samples(*values: int) -> bytes:
    return struct.pack(f"<{len(values)}h", *values)


def test_two_channels_are_interleaved_and_the_shorter_one_padded() -> None:
    caller = samples(1, 2, 3)
    agent = samples(-1)
    data = pcm16_wav([caller, agent], 8000)
    with wave.open(io.BytesIO(data), "rb") as reader:
        assert (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) == (2, 2, 8000)
        assert reader.getnframes() == 3
        frames = reader.readframes(3)
    assert struct.unpack("<6h", frames) == (1, -1, 2, 0, 3, 0)


def test_single_channel() -> None:
    with wave.open(io.BytesIO(pcm16_wav([samples(7, -7)], 16000)), "rb") as reader:
        assert reader.getnchannels() == 1
        assert struct.unpack("<2h", reader.readframes(2)) == (7, -7)


def test_empty_channels_make_an_empty_file() -> None:
    with wave.open(io.BytesIO(pcm16_wav([b"", b""], 8000)), "rb") as reader:
        assert reader.getnframes() == 0


@pytest.mark.parametrize(
    ("channels", "rate", "message"),
    [([], 8000, "at least one channel"), ([b"\x00"], 8000, "odd number of bytes"), ([b""], 0, "positive")],
)
def test_invalid_input(channels: list[bytes], rate: int, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        pcm16_wav(channels, rate)
