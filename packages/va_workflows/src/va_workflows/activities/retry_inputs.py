"""Input, result, timeout, and retry policy of ``schedule_retry_call`` (specs/07-media-pipeline.md §14
Decision P05-T10f).

Workflows import this module to schedule the activity, so it imports only pydantic, the Temporal
``RetryPolicy``, and the error type names.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict
from temporalio.common import RetryPolicy

from va_workflows.activities.errors import CALL_NOT_FOUND

__all__ = ["RETRY_CALL_RETRY", "RETRY_CALL_TIMEOUT", "RetryPlan", "RetrySkip"]

type RetrySkip = Literal[
    "not_outbound", "no_retry", "not_ended", "end_reason", "attempts_exhausted", "already_scheduled"
]
"""Why no new child was created: ``not_outbound`` (only outbound calls are retried; an inbound call never is),
``no_retry`` (the call has no ``retry``), ``not_ended`` (the call is not
terminal), ``end_reason`` (it did not end on voicemail, or on no-answer/busy with ``on_no_answer``),
``attempts_exhausted`` (``max_attempts`` retry calls exist in the chain), ``already_scheduled`` (an earlier
attempt created the child; its id and start time are returned so the workflow starts it idempotently)."""

RETRY_CALL_TIMEOUT: Final = timedelta(seconds=20)
RETRY_CALL_RETRY: Final = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=5,
    non_retryable_error_types=[CALL_NOT_FOUND],
)


class RetryPlan(BaseModel):
    """The retry child to start (``child_call_id``, ``scheduled_for``, ``attempt``), or why there is none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    child_call_id: uuid.UUID | None = None
    scheduled_for: datetime | None = None
    attempt: int | None = None
    reason: RetrySkip | None = None
