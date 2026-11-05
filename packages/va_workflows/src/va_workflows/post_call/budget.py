"""How much of the post-call budget ``store_recording`` may use (specs/18-post-call-analysis.md §1, Decision
P08-FIX-6).

Pure functions over the activities' timeouts and retry policies; workflow-safe (no I/O imports). The copy is
the only step whose retries can wait for minutes (a busy copy lock asks for a retry after the holder's lock
TTL), so it gets a ``schedule_to_close`` deadline that leaves room for the steps after it: billing plus the
two step marks, each at its policy's worst case.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from temporalio.common import RetryPolicy

from va_workflows.activities.post_call_inputs import (
    BILLING_RETRY,
    BILLING_TIMEOUT,
    BOOKKEEPING_RETRY,
    BOOKKEEPING_TIMEOUT,
)

__all__ = ["MIN_STORE_WINDOW", "STORE_RESERVE", "retry_worst_case", "store_window"]

_DEFAULT_MAXIMUM_FACTOR: Final = 100
"""Temporal's default ``maximum_interval`` is 100 x ``initial_interval``."""

MIN_STORE_WINDOW: Final = timedelta(seconds=1)
"""A window shorter than this is no window: the copy is skipped instead of started."""


def retry_worst_case(timeout: timedelta, policy: RetryPolicy) -> timedelta:
    """The longest an activity with ``start_to_close=timeout`` and ``policy`` can take: every attempt runs
    into its timeout and every backoff interval (``initial x coefficient^i``, capped at ``maximum_interval``)
    is waited in full. Raises ``ValueError`` for an unlimited policy (``maximum_attempts < 1``)."""
    attempts = policy.maximum_attempts
    if attempts < 1:
        message = "an unlimited retry policy has no worst case"
        raise ValueError(message)
    cap = policy.maximum_interval or policy.initial_interval * _DEFAULT_MAXIMUM_FACTOR
    backoff = sum(
        (min(policy.initial_interval * policy.backoff_coefficient**i, cap) for i in range(attempts - 1)),
        timedelta(),
    )
    return attempts * timeout + backoff


STORE_RESERVE: Final = retry_worst_case(BILLING_TIMEOUT, BILLING_RETRY) + 2 * retry_worst_case(
    BOOKKEEPING_TIMEOUT, BOOKKEEPING_RETRY
)
"""What must stay of the budget once the copy is over: ``finalize_billing`` and the marks of
``store_recording`` and ``finalize_billing`` at their worst case (365 s + 2 x 80 s = 525 s)."""


def store_window(started: datetime, now: datetime, budget_s: int) -> timedelta | None:
    """The time ``store_recording`` may use from ``now`` (all attempts and retry waits together), ``None``
    when less than ``MIN_STORE_WINDOW`` is left before ``started + budget - STORE_RESERVE``."""
    left = started + timedelta(seconds=budget_s) - STORE_RESERVE - now
    return left if left >= MIN_STORE_WINDOW else None
