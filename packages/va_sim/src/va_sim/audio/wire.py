"""Simulator wire formats for synthetic audio: μ-law at 8 kHz, L16 big-endian at 16 kHz (specs/30 §3.2)."""

from __future__ import annotations

from typing import Final

import numpy as np
from numpy.typing import NDArray

from va_sim.audio.codec import pcm_to_l16be, ulaw_encode

__all__ = ["NARROWBAND_RATE", "WIDEBAND_RATE", "check_sample_rate", "encode_wire", "to_int16"]

NARROWBAND_RATE: Final = 8_000
WIDEBAND_RATE: Final = 16_000


def check_sample_rate(sample_rate: int) -> None:
    if sample_rate not in (NARROWBAND_RATE, WIDEBAND_RATE):
        msg = f"sample_rate must be {NARROWBAND_RATE} or {WIDEBAND_RATE}, got {sample_rate}"
        raise ValueError(msg)


def to_int16(signal: NDArray[np.float64]) -> NDArray[np.int16]:
    """Round and clip a float signal in int16 units."""
    return np.clip(np.rint(signal), -32_768, 32_767).astype(np.int16)


def encode_wire(samples: NDArray[np.int16], sample_rate: int) -> bytes:
    """μ-law bytes at 8 kHz, L16 big-endian bytes at 16 kHz."""
    return ulaw_encode(samples) if sample_rate == NARROWBAND_RATE else pcm_to_l16be(samples)
