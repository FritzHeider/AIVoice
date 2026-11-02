"""Call count of the single-pod capacity flow (specs/30 §6 Decision P05-T17e).

``E2E_CONCURRENT_CALLS`` overrides the default of 50 for exploratory runs; 50 is both the specced per-pod
target and the burst of the `scale` plan's `calls_send` bucket, so larger values would be refused at dispatch.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

__all__ = ["CONCURRENT_CALLS_ENV", "DEFAULT_CONCURRENT_CALLS", "MAX_CONCURRENT_CALLS", "concurrent_calls"]

CONCURRENT_CALLS_ENV: Final = "E2E_CONCURRENT_CALLS"
DEFAULT_CONCURRENT_CALLS: Final = 50
MAX_CONCURRENT_CALLS: Final = 50


def concurrent_calls(environ: Mapping[str, str]) -> int:
    """The number of simultaneous calls to place; raises ``ValueError`` for a value outside 1..50."""
    raw = environ.get(CONCURRENT_CALLS_ENV, "").strip()
    if not raw:
        return DEFAULT_CONCURRENT_CALLS
    try:
        count = int(raw)
    except ValueError:
        count = 0
    if not 1 <= count <= MAX_CONCURRENT_CALLS:
        message = f"{CONCURRENT_CALLS_ENV} must be an integer in 1..{MAX_CONCURRENT_CALLS}, got {raw!r}"
        raise ValueError(message)
    return count
