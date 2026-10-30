"""In-band DTMF tones for simulator tests (specs/30-testing-and-evals.md §3.1)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import numpy as np

from va_sim.audio.codec import dbfs_peak
from va_sim.audio.wire import check_sample_rate, encode_wire, to_int16

__all__ = ["DTMF_FREQS", "synthesize_dtmf"]

_ROWS: Final = (697, 770, 852, 941)
_COLUMNS: Final = (1_209, 1_336, 1_477, 1_633)
_KEYPAD: Final = ("123A", "456B", "789C", "*0#D")
_COMPONENT_DBFS: Final = -10.0

DTMF_FREQS: Final[Mapping[str, tuple[int, int]]] = {
    key: (_ROWS[row], _COLUMNS[column]) for row, keys in enumerate(_KEYPAD) for column, key in enumerate(keys)
}


def synthesize_dtmf(digit: str, ms: int = 120, *, sample_rate: int = 8_000) -> bytes:
    """``ms`` of the DTMF tone for ``digit`` as wire audio (μ-law at 8 kHz, L16 big-endian at 16 kHz).

    Each component (row and column sine) peaks at -10 dBFS.
    """
    if digit not in DTMF_FREQS:
        msg = f"digit must be one of 0-9, *, #, A-D, got {digit!r}"
        raise ValueError(msg)
    if ms <= 0:
        msg = f"ms must be positive, got {ms}"
        raise ValueError(msg)
    check_sample_rate(sample_rate)
    low, high = DTMF_FREQS[digit]
    t = np.arange(ms * sample_rate // 1_000) / sample_rate
    tone = dbfs_peak(_COMPONENT_DBFS) * (np.sin(2 * np.pi * low * t) + np.sin(2 * np.pi * high * t))
    return encode_wire(to_int16(tone), sample_rate)
