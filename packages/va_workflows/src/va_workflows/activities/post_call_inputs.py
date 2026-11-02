"""Inputs, results, timeouts, and retry policies of the ``PostCallWorkflow`` activities
(specs/18-post-call-analysis.md §1, Decision P05-T18b).

Workflows import this module to schedule the activities, so it imports only pydantic, the Temporal
``RetryPolicy``, the import-free domain models, and the error type names.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict
from temporalio.common import RetryPolicy

from va_domain.carrier import CarrierEvent
from va_domain.post_call import PostCallStep
from va_workflows.activities.errors import CALL_NOT_FOUND

__all__ = [
    "BILLING_RETRY",
    "BILLING_TIMEOUT",
    "BOOKKEEPING_RETRY",
    "BOOKKEEPING_TIMEOUT",
    "EMIT_RETRY",
    "EMIT_TIMEOUT",
    "RECORD_RETRY",
    "RECORD_TIMEOUT",
    "BillingResult",
    "CallRecordFacts",
    "CompletedResult",
    "CompletedStatus",
    "PostCallStepInput",
    "SavedRecording",
]


def _policy(maximum_attempts: int, *, initial_s: float = 1.0, maximum_s: float = 10.0) -> RetryPolicy:
    return RetryPolicy(
        initial_interval=timedelta(seconds=initial_s),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=maximum_s),
        maximum_attempts=maximum_attempts,
        non_retryable_error_types=[CALL_NOT_FOUND],
    )


RECORD_TIMEOUT: Final = timedelta(seconds=30)
RECORD_RETRY: Final = _policy(5)
"""``finalize_call_record``: 30 s / 5 attempts (specs/18 §1 step 1)."""
BILLING_TIMEOUT: Final = timedelta(seconds=30)
BILLING_RETRY: Final = _policy(10)
"""``finalize_billing``: 30 s / 10 attempts (step 11)."""
EMIT_TIMEOUT: Final = timedelta(seconds=30)
EMIT_RETRY: Final = _policy(10)
"""``emit_completed``: 30 s / 10 attempts (step 13)."""
BOOKKEEPING_TIMEOUT: Final = timedelta(seconds=10)
BOOKKEEPING_RETRY: Final = _policy(5, initial_s=2.0, maximum_s=30.0)
"""``find_saved_recording``, ``report_recording_missing``, ``mark_postcall_step``: 10 s / 5 attempts."""

type CompletedStatus = Literal["emitted", "already_completed", "not_ended"]
"""``emitted``: ``call.completed`` recorded now; ``already_completed``: an earlier run did (no event);
``not_ended``: the call is not terminal, nothing was written (logged as an error)."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PostCallStepInput(_Model):
    """A completed step to record in ``calls.config.postcall_steps``."""

    call_id: uuid.UUID
    org_id: uuid.UUID
    step: PostCallStep


class CallRecordFacts(_Model):
    """What ``finalize_call_record`` found: the facts the workflow plans the remaining steps with.

    ``connected``: the call was answered; ``recording_wanted``: ``record``, ``post_call_evals``, or
    ``retranscribe`` asks for the recording; ``recording_storage``: the worker has object storage;
    ``done_steps``: the steps an earlier run completed."""

    connected: bool
    recording_wanted: bool
    recording_storage: bool
    done_steps: tuple[str, ...] = ()


class SavedRecording(_Model):
    """The stored ``call.recording.saved`` carrier event of the call (``None`` when none arrived yet)."""

    event: CarrierEvent | None = None


class BillingResult(_Model):
    """The outcome of ``finalize_billing``: the call price, the hold outcome, and how many meter rows this
    attempt posted (0 on a re-run)."""

    price_micros: int
    hold: Literal["captured", "released", "none", "settled"]
    posted: int


class CompletedResult(_Model):
    status: CompletedStatus
