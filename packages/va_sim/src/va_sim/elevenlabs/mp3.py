"""Silent MPEG-1 Layer III frames (specs/30 §3.3 Decision P03-T04: ``mp3_44100_128`` without an encoder).

Every frame is a valid 128 kbps, 44.1 kHz, mono frame whose side information and main data are all zero, so a
decoder plays it as silence. Frame sizes alternate between 417 and 418 bytes (the padding bit is set on the
418-byte ones) so the stream averages exactly 128 000 bit/s.
"""

from __future__ import annotations

from typing import Final

__all__ = ["FRAME_SAMPLES", "MP3_SAMPLE_RATE", "frame_bytes", "frame_start_ms", "silent_frames"]

MP3_SAMPLE_RATE: Final = 44_100
FRAME_SAMPLES: Final = 1_152
_BITRATE: Final = 128_000
# Bytes per frame = 144 · bitrate / sample rate (MPEG-1 Layer III), here 417.96...
_FRAME_BYTES_NUMERATOR: Final = 144 * _BITRATE
_PADDED_FRAME: Final = 418
# Sync word, MPEG-1, Layer III, no CRC | bitrate index 9 (128 kbps), 44.1 kHz, padding bit | mono.
_SYNC: Final = b"\xff\xfb"
_RATE_BYTE: Final = 0x90
_PADDING_BIT: Final = 0x02
_MODE_BYTE: Final = 0xC0
_HEADER_BYTES: Final = 4


def frame_bytes(index: int) -> int:
    """Length of frame ``index`` (0-based): the bytes that keep the running total at 128 kbps."""
    return (
        index + 1
    ) * _FRAME_BYTES_NUMERATOR // MP3_SAMPLE_RATE - index * _FRAME_BYTES_NUMERATOR // MP3_SAMPLE_RATE


def frame_start_ms(index: int) -> float:
    """Start time of frame ``index`` in milliseconds."""
    return index * FRAME_SAMPLES * 1000 / MP3_SAMPLE_RATE


def _frame(index: int) -> bytes:
    size = frame_bytes(index)
    rate = _RATE_BYTE | (_PADDING_BIT if size == _PADDED_FRAME else 0)
    return _SYNC + bytes((rate, _MODE_BYTE)) + bytes(size - _HEADER_BYTES)


def silent_frames(first: int, count: int) -> bytes:
    """``count`` consecutive silent frames starting at frame index ``first``."""
    return b"".join(_frame(index) for index in range(first, first + count))
