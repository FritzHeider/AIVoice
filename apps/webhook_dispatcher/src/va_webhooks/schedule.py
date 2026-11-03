"""Retry schedule and per-attempt timeouts of webhook deliveries (specs/19-events-and-webhooks.md §8, §6).

A delivery gets the first attempt plus eight retries. After failed attempt ``n`` (1-8) the next attempt waits
``RETRY_DELAYS_S[n - 1]`` seconds with ±10 % jitter (cumulative ≈ 24 h); failed attempt 9 is ``dead``.
Rules: specs/19 §8 Decision P06-T03.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Final

__all__ = [
    "BATCH_TIMEOUT_S",
    "DEFAULT_TIMEOUT_S",
    "JITTER",
    "MAX_ATTEMPTS",
    "RETRY_DELAYS_S",
    "Timeouts",
    "retry_delay_s",
    "timeout_for",
]

RETRY_DELAYS_S: Final[tuple[int, ...]] = (10, 30, 120, 600, 1800, 7200, 21600, 54000)
MAX_ATTEMPTS: Final = len(RETRY_DELAYS_S) + 1
JITTER: Final = 0.1
BATCH_TIMEOUT_S: Final = 10.0
DEFAULT_TIMEOUT_S: Final = 15.0
_BATCH_PREFIX: Final = "batch."


def retry_delay_s(attempt: int, rng: random.Random) -> float | None:
    """Seconds to wait after failed attempt ``attempt`` (1-based); ``None`` when the delivery is dead."""
    if attempt < 1:
        msg = f"attempt must be at least 1, got {attempt}"
        raise ValueError(msg)
    if attempt >= MAX_ATTEMPTS:
        return None
    return RETRY_DELAYS_S[attempt - 1] * (1.0 + JITTER * (2.0 * rng.random() - 1.0))


@dataclass(frozen=True, slots=True)
class Timeouts:
    """Whole-request timeout per attempt: batch lifecycle webhooks (specs/19 §6) and every other event."""

    batch_s: float = BATCH_TIMEOUT_S
    default_s: float = DEFAULT_TIMEOUT_S


def timeout_for(event_type: str, timeouts: Timeouts | None = None) -> float:
    """The attempt timeout of a delivery of ``event_type``."""
    chosen = timeouts if timeouts is not None else Timeouts()
    return chosen.batch_s if event_type.startswith(_BATCH_PREFIX) else chosen.default_s
