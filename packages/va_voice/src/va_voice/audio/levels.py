"""Signal levels and gain (specs/07-media-pipeline.md §10.2).

dBFS is RMS relative to full scale 32 767 (the convention of ``scripts/generate_audio_assets.py``, which
levels the background tracks): a full-scale square wave is 0 dBFS, a full-scale sine about -3.01 dBFS, and
silence or an empty signal is ``-inf``. Float input is treated as already normalised to full scale 1.0.
"""

from __future__ import annotations

import math
from typing import Final

import numpy as np
from numpy.typing import NDArray

from va_voice.audio.codec import FULL_SCALE

__all__ = ["apply_gain", "dbfs_to_amplitude", "gain_for_db", "rms", "rms_dbfs"]

_INT16_MIN: Final = -32_768
_INT16_MAX: Final = 32_767


def rms(x: NDArray[np.int16] | NDArray[np.float32]) -> float:
    """Root mean square (sample units for int16); 0.0 for an empty signal."""
    if x.size == 0:
        return 0.0
    values = np.asarray(x, dtype=np.float64)
    return math.sqrt(float(np.mean(values * values)))


def rms_dbfs(x: NDArray[np.int16] | NDArray[np.float32]) -> float:
    """RMS level in dBFS; ``-inf`` for silence or an empty signal."""
    level = rms(x)
    if level == 0.0:
        return -math.inf
    full_scale = float(FULL_SCALE) if np.issubdtype(x.dtype, np.integer) else 1.0
    return 20.0 * math.log10(level / full_scale)


def gain_for_db(db: float) -> float:
    """The linear amplitude factor of a ``db`` change."""
    return float(10.0 ** (db / 20.0))


def dbfs_to_amplitude(dbfs: float) -> float:
    """The int16 amplitude (or RMS) of a level in dBFS."""
    return FULL_SCALE * gain_for_db(dbfs)


def apply_gain(x: NDArray[np.int16], gain: float) -> NDArray[np.int16]:
    """``x`` scaled by the linear ``gain``, rounded and clipped to the int16 range."""
    scaled = np.rint(np.asarray(x, dtype=np.float32) * np.float32(gain))
    return np.clip(scaled, _INT16_MIN, _INT16_MAX).astype(np.int16)
