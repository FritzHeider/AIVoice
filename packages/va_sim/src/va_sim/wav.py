"""PCM16 WAV encoding for simulator recordings (specs/30-testing-and-evals.md §3.2 recordings)."""

from __future__ import annotations

import io
import wave
from collections.abc import Sequence
from typing import Final

__all__ = ["pcm16_wav"]

_SAMPLE_WIDTH: Final = 2


def pcm16_wav(channels: Sequence[bytes], sample_rate: int) -> bytes:
    """A WAV file interleaving ``channels`` (PCM16 little-endian); shorter channels get silence padding."""
    if not channels:
        msg = "a WAV file needs at least one channel"
        raise ValueError(msg)
    if sample_rate <= 0:
        msg = f"sample rate must be positive, got {sample_rate}"
        raise ValueError(msg)
    for index, channel in enumerate(channels):
        if len(channel) % _SAMPLE_WIDTH:
            msg = f"channel {index} has an odd number of bytes ({len(channel)}); PCM16 needs whole samples"
            raise ValueError(msg)
    frames = max(len(channel) for channel in channels) // _SAMPLE_WIDTH
    padded = [channel.ljust(frames * _SAMPLE_WIDTH, b"\x00") for channel in channels]
    interleaved = bytearray(frames * _SAMPLE_WIDTH * len(channels))
    stride = _SAMPLE_WIDTH * len(channels)
    for index, channel in enumerate(padded):
        offset = index * _SAMPLE_WIDTH
        interleaved[offset::stride] = channel[0::_SAMPLE_WIDTH]
        interleaved[offset + 1 :: stride] = channel[1::_SAMPLE_WIDTH]
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(len(channels))
        writer.setsampwidth(_SAMPLE_WIDTH)
        writer.setframerate(sample_rate)
        writer.writeframes(bytes(interleaved))
    return buffer.getvalue()
