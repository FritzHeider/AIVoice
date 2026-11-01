"""Sample-rate conversion with soxr (specs/07-media-pipeline.md §4).

The VAD runs at 16 kHz, so the 8 kHz PCMU stream is upsampled per call with one stateful
``soxr.ResampleStream(8000, 16000, 1, dtype="int16", quality="LQ")``. soxr ships no type information, so
this module is the only place that touches it and every result is converted to a typed int16 array.
"""

from __future__ import annotations

from typing import Final, Literal

import numpy as np
import soxr
from numpy.typing import NDArray

__all__ = ["Quality", "Resampler", "resample"]

type Quality = Literal["QQ", "LQ", "MQ", "HQ", "VHQ"]

_EMPTY: Final[NDArray[np.int16]] = np.zeros(0, dtype=np.int16)


def _int16(x: object) -> NDArray[np.int16]:
    return np.asarray(x, dtype=np.int16)


class Resampler:
    """Streaming int16 mono resampler; output lags the input by the filter delay until ``flush``."""

    __slots__ = ("_flushed", "_in_rate", "_out_rate", "_stream")

    def __init__(self, in_rate: int = 8000, out_rate: int = 16000, *, quality: Quality = "LQ") -> None:
        self._in_rate = in_rate
        self._out_rate = out_rate
        self._stream = soxr.ResampleStream(in_rate, out_rate, 1, dtype="int16", quality=quality)
        self._flushed = False

    @property
    def in_rate(self) -> int:
        return self._in_rate

    @property
    def out_rate(self) -> int:
        return self._out_rate

    def process(self, x: NDArray[np.int16]) -> NDArray[np.int16]:
        """Resample the next chunk; returns the output samples that are ready."""
        if self._flushed:
            msg = "resampler already flushed"
            raise RuntimeError(msg)
        return _int16(self._stream.resample_chunk(np.ascontiguousarray(x, dtype=np.int16)))

    def flush(self) -> NDArray[np.int16]:
        """The samples still held by the filter; the resampler accepts no more input afterwards."""
        if self._flushed:
            msg = "resampler already flushed"
            raise RuntimeError(msg)
        self._flushed = True
        return _int16(self._stream.resample_chunk(_EMPTY, last=True))


def resample(
    x: NDArray[np.int16], in_rate: int, out_rate: int, *, quality: Quality = "LQ"
) -> NDArray[np.int16]:
    """One-shot resampling of a whole int16 signal."""
    return _int16(soxr.resample(np.ascontiguousarray(x, dtype=np.int16), in_rate, out_rate, quality=quality))
