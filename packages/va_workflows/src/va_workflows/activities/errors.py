"""Temporal ``ApplicationError`` types the call activities raise; workflows name them in retry policies.

Kept free of I/O imports so workflow modules can import the type names.
"""

from __future__ import annotations

from typing import Final

from temporalio.exceptions import ApplicationError

__all__ = [
    "BILLING_UNAVAILABLE",
    "CALL_NOT_ENDED",
    "CALL_NOT_FOUND",
    "DISPATCH_UNAVAILABLE",
    "EVENTS_UNAVAILABLE",
    "HANGUP_REJECTED",
    "INVALID_END_REASON",
    "STATE_UNAVAILABLE",
    "STORAGE_UNAVAILABLE",
    "TELNYX_UNAVAILABLE",
    "call_not_found",
    "retryable",
]

BILLING_UNAVAILABLE: Final = "BillingUnavailable"
"""The plan or pricing data needed to price a call could not be read (retried)."""
CALL_NOT_ENDED: Final = "CallNotEnded"
"""A post-call step found the call in a non-terminal status (retried: the end is being recorded)."""
CALL_NOT_FOUND: Final = "CallNotFound"
"""The call does not exist in the org (never retried)."""
DISPATCH_UNAVAILABLE: Final = "DispatchUnavailable"
"""The limit counters or the billing data could not be read at dispatch (retried)."""
EVENTS_UNAVAILABLE: Final = "EventsUnavailable"
"""The call event ``seq`` could not be assigned in Redis; the transaction was rolled back (retried)."""
HANGUP_REJECTED: Final = "HangupRejected"
"""Telnyx refused the hangup with an error other than "call already ended" (never retried)."""
INVALID_END_REASON: Final = "InvalidEndReason"
"""The workflow asked for an end reason that cannot apply to the call's phase (never retried)."""
STATE_UNAVAILABLE: Final = "StateUnavailable"
"""The call's Redis carrier state could not be read (retried)."""
STORAGE_UNAVAILABLE: Final = "StorageUnavailable"
"""The object store (S3/MinIO) refused or failed an upload (retried)."""
TELNYX_UNAVAILABLE: Final = "TelnyxUnavailable"
"""The Telnyx request hit the client rate limit or got no usable response (retried; same ``command_id``)."""


def call_not_found() -> ApplicationError:
    return ApplicationError("call not found", type=CALL_NOT_FOUND, non_retryable=True)


def retryable(message: str, error_type: str) -> ApplicationError:
    """A failure Temporal retries under the activity's retry policy."""
    return ApplicationError(message, type=error_type)
