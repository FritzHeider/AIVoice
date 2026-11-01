"""G.711 μ-law, L16 and PCM16 conversions for the media pipeline (specs/07-media-pipeline.md §4, §5.3, §15).

Both μ-law directions are lookup tables computed once at import: ``ULAW_TO_PCM16`` maps each of the 256
codes to its int16 sample and ``PCM16_TO_ULAW`` maps every int16 sample (indexed by ``sample + 32768``) to
its code (ITU-T G.711: bias 0x84, magnitude clipped at 32 635, code bits inverted). The per-frame work is a
single numpy fancy-index. L16 travels big-endian on the Telnyx wire by default (``TELNYX_L16_BYTE_ORDER``),
while ElevenLabs ``pcm_16000`` and the widget use little-endian PCM16; the VAD consumes float32 in [-1, 1).
"""

from __future__ import annotations

from typing import Final, Literal

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "FULL_SCALE",
    "PCM16_TO_ULAW",
    "ULAW_SILENCE",
    "ULAW_TO_PCM16",
    "L16ByteOrder",
    "float32_to_pcm",
    "l16_to_pcm",
    "l16be_to_pcm",
    "pcm16le_to_pcm",
    "pcm_to_float32",
    "pcm_to_l16",
    "pcm_to_l16be",
    "pcm_to_pcm16le",
    "ulaw_decode",
    "ulaw_encode",
]

type L16ByteOrder = Literal["big", "little"]

ULAW_SILENCE: Final = 0xFF
"""The μ-law code of a zero sample (codec silence, used to pad frames)."""

FULL_SCALE: Final = 32_767
"""The int16 amplitude of 0 dBFS."""

_ULAW_BIAS: Final = 0x84
_ULAW_CLIP: Final = 32_635
_ULAW_SIGN: Final = 0x80
_INT16_OFFSET: Final = 32_768
_FLOAT_SCALE: Final = 32_768.0
_DTYPES: Final[dict[str, str]] = {"big": ">i2", "little": "<i2"}


def _build_decode_table() -> NDArray[np.int16]:
    codes = np.bitwise_not(np.arange(256, dtype=np.uint8)).astype(np.int32)
    exponent = np.right_shift(codes, 4) & 0x07
    mantissa = codes & 0x0F
    magnitude = np.left_shift(2 * mantissa + 33, exponent + 2) - _ULAW_BIAS
    sign = np.where(codes & _ULAW_SIGN, -1, 1)
    table: NDArray[np.int16] = (sign * magnitude).astype(np.int16)
    return table


def _build_encode_table() -> NDArray[np.uint8]:
    samples = np.arange(-_INT16_OFFSET, _INT16_OFFSET, dtype=np.int32)
    biased = np.clip(np.abs(samples), 0, _ULAW_CLIP) + _ULAW_BIAS
    # Segment boundaries of the biased magnitude: 0x100, 0x200, ..., 0x4000 (exponents 1..7).
    boundaries = np.left_shift(np.int32(0x100), np.arange(7, dtype=np.int32))
    exponent = np.searchsorted(boundaries, biased, side="right").astype(np.int32)
    mantissa = np.right_shift(biased, exponent + 3) & 0x0F
    sign = np.where(samples < 0, _ULAW_SIGN, 0x00)
    table: NDArray[np.uint8] = np.bitwise_not((sign | (exponent << 4) | mantissa).astype(np.uint8))
    return table


ULAW_TO_PCM16: Final[NDArray[np.int16]] = _build_decode_table()
PCM16_TO_ULAW: Final[NDArray[np.uint8]] = _build_encode_table()


def ulaw_decode(b: bytes) -> NDArray[np.int16]:
    """int16 samples of μ-law bytes."""
    decoded: NDArray[np.int16] = ULAW_TO_PCM16[np.frombuffer(b, dtype=np.uint8)]
    return decoded


def ulaw_encode(x: NDArray[np.int16]) -> bytes:
    """μ-law bytes of int16 samples."""
    index = np.asarray(x, dtype=np.int16).astype(np.int32) + _INT16_OFFSET
    return PCM16_TO_ULAW[index].tobytes()


def _whole_samples(b: bytes) -> None:
    if len(b) % 2:
        msg = f"16-bit PCM needs whole samples; got {len(b)} bytes"
        raise ValueError(msg)


def l16_to_pcm(b: bytes, byte_order: L16ByteOrder) -> NDArray[np.int16]:
    """Native int16 samples of 16-bit PCM bytes in ``byte_order``."""
    _whole_samples(b)
    return np.frombuffer(b, dtype=_DTYPES[byte_order]).astype(np.int16)


def pcm_to_l16(x: NDArray[np.int16], byte_order: L16ByteOrder) -> bytes:
    """16-bit PCM bytes in ``byte_order`` for int16 samples."""
    return np.asarray(x, dtype=np.int16).astype(_DTYPES[byte_order]).tobytes()


def l16be_to_pcm(b: bytes) -> NDArray[np.int16]:
    """int16 samples of L16 big-endian wire bytes (hd_audio)."""
    return l16_to_pcm(b, "big")


def pcm_to_l16be(x: NDArray[np.int16]) -> bytes:
    """L16 big-endian wire bytes of int16 samples (hd_audio)."""
    return pcm_to_l16(x, "big")


def pcm16le_to_pcm(b: bytes) -> NDArray[np.int16]:
    """int16 samples of little-endian PCM16 (ElevenLabs ``pcm_16000``, widget audio)."""
    return l16_to_pcm(b, "little")


def pcm_to_pcm16le(x: NDArray[np.int16]) -> bytes:
    """Little-endian PCM16 bytes of int16 samples."""
    return pcm_to_l16(x, "little")


def pcm_to_float32(x: NDArray[np.int16]) -> NDArray[np.float32]:
    """float32 samples in [-1, 1) (``x / 32768``), the Silero VAD input."""
    scaled: NDArray[np.float32] = np.asarray(x, dtype=np.int16).astype(np.float32) / np.float32(_FLOAT_SCALE)
    return scaled


def float32_to_pcm(x: NDArray[np.float32]) -> NDArray[np.int16]:
    """int16 samples of float audio (``rint(x · 32768)``, clipped to the int16 range)."""
    scaled = np.rint(np.asarray(x, dtype=np.float64) * _FLOAT_SCALE)
    return np.clip(scaled, -_INT16_OFFSET, _INT16_OFFSET - 1).astype(np.int16)
