"""Voice clone sample checks (Decision P03-T05c rule 7). Pure: no I/O, safe in a worker thread.

A sample is detected from its bytes. RIFF/WAVE (stdlib ``wave``), MP3 (optional ID3v2 tag, then MPEG-1/2/2.5
Layer III frames walked to the first non-header), and FLAC (STREAMINFO) are measured; Ogg, MP4/M4A, and WebM
are accepted unmeasured. Anything else, or a broken WAV/MP3/FLAC, is unreadable. Samples over 25 MB or
measured under 1 second are rejected.
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from typing import Final

__all__ = ["MAX_SAMPLE_BYTES", "MIN_SAMPLE_S", "SampleError", "SampleInfo", "measure"]

MAX_SAMPLE_BYTES: Final = 25 * 1024 * 1024
MIN_SAMPLE_S: Final = 1.0

_ID3_HEADER: Final = 10
_ID3_FOOTER_FLAG: Final = 0x10
_MP3_HEADER: Final = 4
_SYNC_BYTE: Final = 0xFF
_SYNC_BITS: Final = 0xE0
_FLAC_STREAMINFO_END: Final = 42
_FLAC_TOTAL_BITS: Final = 36
_FLAC_RATE_SHIFT: Final = 44
_LAYER_III: Final = 1
_MPEG1: Final = 3
_RESERVED_VERSION: Final = 1
_BAD_BITRATE: Final = 15
_BAD_RATE: Final = 3
# Layer III bitrates (kbps) by bitrate index; index 0 (free format) is not measurable.
_BITRATES_V1: Final = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_BITRATES_V2: Final = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
# Sample rates by version bits (0 = MPEG-2.5, 2 = MPEG-2, 3 = MPEG-1) and rate index.
_RATES: Final = {0: (11_025, 12_000, 8_000), 2: (22_050, 24_000, 16_000), 3: (44_100, 48_000, 32_000)}
_UNMEASURED: Final = (
    (b"OggS", 0, "audio/ogg"),
    (b"ftyp", 4, "audio/mp4"),
    (b"\x1a\x45\xdf\xa3", 0, "audio/webm"),
)


@dataclass(frozen=True, slots=True)
class SampleInfo:
    mime_type: str
    duration_s: float | None


class SampleError(Exception):
    """The sample is rejected; ``message`` is the ``invalid_sample`` detail message."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _UnreadableError(Exception):
    """The bytes are not audio the simulator can read (mapped to the ``could not be read`` message)."""


def _wav_duration(data: bytes) -> float:
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            frames, rate = reader.getnframes(), reader.getframerate()
    except (wave.Error, EOFError) as exc:
        raise _UnreadableError from exc
    if rate <= 0:
        raise _UnreadableError
    return frames / rate


def _id3_end(data: bytes) -> int:
    """Offset of the first byte after an ID3v2 tag (0 without one)."""
    if not data.startswith(b"ID3"):
        return 0
    if len(data) < _ID3_HEADER:
        raise _UnreadableError
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | (byte & 0x7F)
    footer = _ID3_HEADER if data[5] & _ID3_FOOTER_FLAG else 0
    return _ID3_HEADER + size + footer


def _has_sync(data: bytes, offset: int) -> bool:
    return (
        offset + _MP3_HEADER <= len(data)
        and data[offset] == _SYNC_BYTE
        and data[offset + 1] & _SYNC_BITS == _SYNC_BITS
    )


def _mp3_frame(data: bytes, offset: int) -> tuple[int, int, int] | None:
    """``(frame length, samples, sample rate)`` of the Layer III frame header at ``offset``, else ``None``."""
    if not _has_sync(data, offset):
        return None
    version = (data[offset + 1] >> 3) & 0x03
    layer = (data[offset + 1] >> 1) & 0x03
    bitrate_index = data[offset + 2] >> 4
    rate_index = (data[offset + 2] >> 2) & 0x03
    if version == _RESERVED_VERSION or layer != _LAYER_III:
        return None
    if bitrate_index in {0, _BAD_BITRATE} or rate_index == _BAD_RATE:
        return None
    mpeg1 = version == _MPEG1
    bitrate = (_BITRATES_V1 if mpeg1 else _BITRATES_V2)[bitrate_index] * 1000
    rate = _RATES[version][rate_index]
    padding = (data[offset + 2] >> 1) & 0x01
    length = (144 if mpeg1 else 72) * bitrate // rate + padding
    return length, 1152 if mpeg1 else 576, rate


def _mp3_duration(data: bytes) -> float:
    offset = _id3_end(data)
    duration = 0.0
    frames = 0
    while (frame := _mp3_frame(data, offset)) is not None and offset + frame[0] <= len(data):
        length, samples, rate = frame
        duration += samples / rate
        offset += length
        frames += 1
    if frames == 0:
        raise _UnreadableError
    return duration


def _flac_duration(data: bytes) -> float | None:
    if len(data) < _FLAC_STREAMINFO_END or data[4] & 0x7F != 0:
        raise _UnreadableError
    packed = int.from_bytes(data[18:26], "big")
    rate = packed >> _FLAC_RATE_SHIFT
    total = packed & ((1 << _FLAC_TOTAL_BITS) - 1)
    if rate == 0:
        raise _UnreadableError
    return total / rate if total else None


def _is_mp3(data: bytes) -> bool:
    return data.startswith(b"ID3") or _mp3_frame(data, 0) is not None


def _detect(data: bytes) -> SampleInfo:
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return SampleInfo("audio/wav", _wav_duration(data))
    if data.startswith(b"fLaC"):
        return SampleInfo("audio/flac", _flac_duration(data))
    if _is_mp3(data):
        return SampleInfo("audio/mpeg", _mp3_duration(data))
    for magic, offset, mime_type in _UNMEASURED:
        if data[offset : offset + len(magic)] == magic:
            return SampleInfo(mime_type, None)
    raise _UnreadableError


def measure(file_name: str, data: bytes) -> SampleInfo:
    """The sample's type and duration (``None`` when unmeasured); :class:`SampleError` when rejected."""
    if len(data) > MAX_SAMPLE_BYTES:
        msg = f"Sample '{file_name}' is larger than {MAX_SAMPLE_BYTES // (1024 * 1024)} MB"
        raise SampleError(msg)
    try:
        info = _detect(data)
    except _UnreadableError:
        msg = f"Sample '{file_name}' could not be read as audio"
        raise SampleError(msg) from None
    if info.duration_s is not None and info.duration_s < MIN_SAMPLE_S:
        msg = f"Sample '{file_name}' is shorter than 1 second"
        raise SampleError(msg)
    return info
