"""ElevenLabs REST retry schedules (Decision 2026-11-01 P03-T12 rule 3; specs/08 §8, specs/09 §9).

A schedule lists the waits before each retry; every wait gets a uniform 0-100 ms jitter. Retries happen on
``429``/``500``/``502``/``503``/``504`` and on transport failures (timeouts, connection errors). A
``Retry-After`` header longer than the scheduled wait replaces it; one longer than 30 s ends the retries.
Operations that are not idempotent (``add_voice``, ``edit_voice``) use :data:`NO_RETRY`: the clone workflow
owns their retries (specs/15 §3.2).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Final

__all__ = ["BATCH_RETRY", "NO_RETRY", "READ_RETRY", "RETRYABLE_STATUSES", "TTS_RETRY", "RestRetry"]

RETRYABLE_STATUSES: Final = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class RestRetry:
    """Waits (seconds) before each retry, the jitter bound, the retryable statuses, and the ``Retry-After``
    cap."""

    delays_s: tuple[float, ...]
    jitter_s: float = 0.1
    statuses: frozenset[int] = field(default=RETRYABLE_STATUSES)
    retry_after_cap_s: float = 30.0

    def wait(self, retry: int, rng: random.Random, retry_after_s: float | None = None) -> float | None:
        """Seconds before retry number ``retry`` (0-based), or ``None`` when no retry follows."""
        if retry >= len(self.delays_s):
            return None
        if retry_after_s is not None and retry_after_s > self.retry_after_cap_s:
            return None
        scheduled = self.delays_s[retry] + rng.uniform(0.0, self.jitter_s)
        return scheduled if retry_after_s is None else max(scheduled, retry_after_s)


READ_RETRY: Final = RestRetry((0.5, 1.0, 2.0))
TTS_RETRY: Final = RestRetry((0.2,))
BATCH_RETRY: Final = RestRetry((5.0, 20.0))
NO_RETRY: Final = RestRetry(())
