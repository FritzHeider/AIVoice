"""Telnyx retry policy (specs/06-telephony-telnyx.md §2.2, Decision 2026-11-01 P03-T08a).

Up to three retries after the first request, waiting 100 ms, 400 ms, and 1 200 ms plus a uniform 0-100 ms
jitter. A ``Retry-After`` header (delta seconds or an HTTP-date) replaces the delay when it is longer; one
longer than 5 s ends the retries (the error is mapped as is).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final

__all__ = ["RETRY_STATUSES", "RetryPolicy", "retry_after_seconds"]

RETRY_STATUSES: Final = frozenset({429, 500, 502, 503, 504})


def retry_after_seconds(value: str | None, now: datetime) -> float | None:
    """Seconds a ``Retry-After`` header asks for (``None`` when absent or unparsable; never negative)."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    if text.isdigit():
        return float(int(text))
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now).total_seconds())


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Delays (seconds) before each retry, the jitter bound, the retryable statuses, and the ``Retry-After``
    cap."""

    delays_s: tuple[float, ...] = (0.1, 0.4, 1.2)
    jitter_s: float = 0.1
    retry_statuses: frozenset[int] = field(default=RETRY_STATUSES)
    max_retry_after_s: float = 5.0

    @property
    def max_retries(self) -> int:
        return len(self.delays_s)

    def delay(self, retry_index: int, rng: random.Random, retry_after_s: float | None = None) -> float | None:
        """Seconds to wait before retry ``retry_index`` (0 = the first retry), or ``None`` to stop
        retrying."""
        if retry_index >= len(self.delays_s):
            return None
        if retry_after_s is not None and retry_after_s > self.max_retry_after_s:
            return None
        base = self.delays_s[retry_index] + rng.uniform(0.0, self.jitter_s)
        return base if retry_after_s is None else max(base, retry_after_s)
