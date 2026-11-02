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

PREFETCH_TIMEOUT: Final = timedelta(seconds=10)
PREFETCH_RETRY: Final = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=5),
    maximum_attempts=3,
    non_retryable_error_types=[CALL_NOT_FOUND],
)


class PrefetchResult(BaseModel):
    """``stored`` tells whether a context was saved, ``status`` is the call's status when it was read, and
    ``ttl_s`` the context's Redis TTL when stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stored: bool
    status: str
    ttl_s: int | None = None
