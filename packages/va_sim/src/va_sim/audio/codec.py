"""G.711 μ-law and L16 helpers for the simulators (specs/07-media-pipeline.md codec, specs/30 §3.1).

The simulator owns its codec (it must not share audio construction with ``va_voice``), built table-first as
specs/07 describes: ``ULAW_TO_PCM16`` decodes every code, ``PCM16_TO_ULAW`` encodes every int16 sample
indexed by ``sample + 32768``. Both follow ITU-T G.711 μ-law with bias 0x84, inverted code bits, and clipping
at 32 635.
"""

from __future__ import annotations

from typing import Final

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "PCM16_TO_ULAW",
    "ULAW_SILENCE",
    "ULAW_TO_PCM16",
    "dbfs_peak",
    "l16be_to_pcm",
    "pcm_to_l16be",
    "ulaw_decode",
    "ulaw_encode",
]

_BIAS: Final = 0x84
_CLIP: Final = 32_635
_SEGMENTS: Final = 8
_SIGN_BIT: Final = 0x80
_SEGMENT_FLOOR: Final = 0x80  # biased magnitudes in segment s lie in [0x80 << s, 0x80 << (s + 1))
_FULL_SCALE: Final = 32_767

ULAW_SILENCE: Final = 0xFF


def _decode_table() -> NDArray[np.int16]:
    """Each code: invert, split sign/segment/step, rebuild ``((step·8 + bias) << segment) - bias``."""
    inverted = np.arange(256, dtype=np.int32) ^ 0xFF
    segment = (inverted & 0x70) >> 4
    step = inverted & 0x0F
    magnitude = ((step * 8 + _BIAS) << segment) - _BIAS
    signed = np.where(inverted & _SIGN_BIT, -magnitude, magnitude)
    return signed.astype(np.int16)


def _encode_table() -> NDArray[np.uint8]:
    """Each int16 sample: biased magnitude → segment from its top bit → 4 step bits → invert."""
    sample = np.arange(-32_768, 32_768, dtype=np.int32)
    negative = sample < 0
    biased = np.minimum(np.abs(sample), _CLIP) + _BIAS
    segment = np.zeros_like(biased)
    for boundary in range(1, _SEGMENTS):
        segment += biased >= _SEGMENT_FLOOR << boundary
    step = (biased >> (segment + 3)) & 0x0F
    code = (segment << 4) | step | np.where(negative, _SIGN_BIT, 0)
    codes: NDArray[np.uint8] = (code ^ 0xFF).astype(np.uint8)
    return codes


ULAW_TO_PCM16: Final[NDArray[np.int16]] = _decode_table()
PCM16_TO_ULAW: Final[NDArray[np.uint8]] = _encode_table()


def ulaw_encode(x: NDArray[np.int16]) -> bytes:
    """μ-law bytes for int16 samples."""
    index = np.asarray(x, dtype=np.int16).astype(np.int32) + 32_768
    return bytes(PCM16_TO_ULAW[index].tobytes())


def ulaw_decode(b: bytes) -> NDArray[np.int16]:
    """int16 samples for μ-law bytes."""
    codes = np.frombuffer(b, dtype=np.uint8)
    decoded: NDArray[np.int16] = ULAW_TO_PCM16[codes]
    return decoded


def pcm_to_l16be(x: NDArray[np.int16]) -> bytes:
    """L16 wire bytes (big-endian int16, the hd_audio format)."""
    return bytes(np.asarray(x, dtype=np.int16).astype(">i2").tobytes())


def l16be_to_pcm(b: bytes) -> NDArray[np.int16]:
    """int16 samples for L16 big-endian wire bytes."""
    return np.frombuffer(b, dtype=">i2").astype(np.int16)


def dbfs_peak(dbfs: float) -> int:
    """The int16 amplitude of a peak at ``dbfs`` (0 dBFS = 32 767)."""
    return round(_FULL_SCALE * 10 ** (dbfs / 20))
