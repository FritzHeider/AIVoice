"""Seeded randomness for synthetic audio: raw PCG64 output only (AGENTS.md determinism rule)."""

from __future__ import annotations

from typing import Final

import numpy as np
from numpy.typing import NDArray

__all__ = ["AudioRng"]

_MANTISSA_SHIFT: Final = np.uint64(11)
_UNIT: Final = 2.0**-53


class AudioRng:
    """Uniform floats in [0, 1) from the top 53 bits of each raw 64-bit PCG64 draw."""

    def __init__(self, seed: int) -> None:
        if seed < 0:
            msg = f"audio seed must be non-negative, got {seed}"
            raise ValueError(msg)
        self._generator = np.random.PCG64(seed)

    def uniform(self, n: int) -> NDArray[np.float64]:
        draws = np.asarray(self._generator.random_raw(n), dtype=np.uint64)
        scaled: NDArray[np.float64] = (draws >> _MANTISSA_SHIFT).astype(np.float64) * _UNIT
        return scaled
