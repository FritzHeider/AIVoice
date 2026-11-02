"""Inputs, results, timeouts, and retry policies of the telephony activities (``dial``, ``hangup_call``,
``end_call``; specs/06-telephony-telnyx.md §6 Decision P05-T06b).

Workflows import this module to schedule the activities, so it imports only pydantic, the Temporal
``RetryPolicy``, and the error type names.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict
from temporalio.common import RetryPolicy

from va_workflows.activities.errors import CALL_NOT_FOUND, HANGUP_REJECTED, INVALID_END_REASON
from va_workflows.activities.inputs import CallActivityInput

__all__ = [
    "DIAL_RETRY",
    "DIAL_TIMEOUT",
    "END_CALL_RETRY",
    "END_CALL_TIMEOUT",
    "HANGUP_RETRY",
    "HANGUP_TIMEOUT",
    "PRE_ANSWER_ONLY_REASONS",
    "DialInput",
    "DialOutcome",
    "DialResultKind",
    "EndCallInput",
    "EndCallResult",
    "HangupInput",
    "HangupResult",
    "WorkflowEndReason",
    "max_duration_seconds",
]

type DialResultKind = Literal["dialing", "already_dialed", "skipped", "requeue", "blocked", "failed"]
"""``dialing``: the dial went out now; ``already_dialed``: an earlier attempt reached Telnyx; ``skipped``: the
call was stopped meanwhile; ``requeue``: the concurrency cap is full, try again later; ``blocked``: a dispatch
re-check failed (the call is ``failed``/``dispatch_blocked``); ``failed``: Telnyx rejected the dial."""

type WorkflowEndReason = Literal[
    "canceled",
    "dispatch_blocked",
    "carrier_error",
    "error",
    "worker_crash",
    "worker_lost",
    "fax_detected",
    "max_duration",
]
"""The ``end_reason`` values ``CallWorkflow`` itself records (specs/05 §9); ``worker_lost`` after the media
worker lost the call (ownership loss or drain timeout) without recording or hanging up."""

PRE_ANSWER_ONLY_REASONS: Final[frozenset[WorkflowEndReason]] = frozenset(
    {"canceled", "dispatch_blocked", "carrier_error"}
)
"""Reasons that only end a call that was never answered."""


def _policy(maximum_attempts: int, non_retryable: list[str]) -> RetryPolicy:
    """1 s initial interval, doubling, capped at 10 s."""
    return RetryPolicy(
        initial_interval=timedelta(seconds=1),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=10),
        maximum_attempts=maximum_attempts,
        non_retryable_error_types=non_retryable,
    )


DIAL_TIMEOUT: Final = timedelta(seconds=30)
DIAL_RETRY: Final = _policy(5, [CALL_NOT_FOUND, INVALID_END_REASON])
HANGUP_TIMEOUT: Final = timedelta(seconds=15)
HANGUP_RETRY: Final = _policy(6, [HANGUP_REJECTED])
END_CALL_TIMEOUT: Final = timedelta(seconds=15)
END_CALL_RETRY: Final = _policy(6, [CALL_NOT_FOUND, INVALID_END_REASON])


def max_duration_seconds(max_duration_min: float) -> int:
    """``max_duration`` (minutes) in whole seconds, rounded up."""
    return math.ceil(max_duration_min * 60)


class _Result(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DialInput(CallActivityInput):
    """``final_attempt``: the workflow's concurrency requeue budget is spent; a full cap blocks the call."""

    final_attempt: bool = False


class DialOutcome(_Result):
    """What ``dial`` did; ``status`` is the call's status afterwards, ``code`` the dispatch error code of a
    ``blocked``/``requeue`` outcome or the Telnyx error code of a ``failed`` one. ``max_duration_s`` (the
    call's ``max_duration`` in whole seconds, rounded up) is set for ``dialing``/``already_dialed`` so the
    workflow can arm its backstop without reading the configuration."""

    result: DialResultKind
    status: str
    call_control_id: str | None = None
    code: str | None = None
    max_duration_s: int | None = None


class HangupInput(CallActivityInput):
    """``reason`` is logged with the hangup (``fax_detected``, ``stream_failed``, ``worker_crash``, ...)."""

    reason: str


class HangupResult(_Result):
    sent: bool
    reason: Literal["sent", "no_leg", "already_ended"]


class EndCallInput(CallActivityInput):
    """``code`` overrides ``data.reason`` of the ``call.dispatch_blocked`` event (default
    ``dispatch_blocked``); ``error_message`` is stored on the call."""

    end_reason: WorkflowEndReason
    error_message: str | None = None
    code: str | None = None


class EndCallResult(_Result):
    """``changed``: the end was recorded now; ``status`` is the call's status afterwards."""

    changed: bool
    status: str
