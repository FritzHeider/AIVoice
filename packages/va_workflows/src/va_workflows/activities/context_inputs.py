"""Result, timeout, and retry policy of ``prefetch_call_context`` (specs/01-architecture.md §2.1 step 2).

Workflows import this module to schedule the activity, so it imports only pydantic, the Temporal
``RetryPolicy``, and the error type names.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict
from temporalio.common import RetryPolicy

from va_workflows.activities.errors import CALL_NOT_FOUND

__all__ = ["PREFETCH_RETRY", "PREFETCH_TIMEOUT", "PrefetchResult"]

PREFETCH_TIMEOUT: Final = timedelta(seconds=20)
"""Covers the opener pre-render (8 s budget, specs/07 §11 Decision P05-T10d) on top of the context build."""
PREFETCH_RETRY: Final = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=5),
    maximum_attempts=3,
    non_retryable_error_types=[CALL_NOT_FOUND],
)


class PrefetchResult(BaseModel):
    """``stored`` tells whether a context was saved, ``status`` is the call's status when it was read,
    ``ttl_s`` the context's Redis TTL when stored, and ``opener_cached`` whether the opener is in the phrase
    cache."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stored: bool
    status: str
    ttl_s: int | None = None
    opener_cached: bool = False
