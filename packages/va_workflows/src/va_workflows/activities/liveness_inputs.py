"""Inputs, results, timeouts, and retry policies of the liveness activities (``watch_call_heartbeat``,
``call_presence``; specs/01-architecture.md §2.1 Decision P05-T06c, specs/07 §19).

Workflows import this module to schedule the activities, so it imports only pydantic, the Temporal
``RetryPolicy``, and the shared activity input.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field
from temporalio.common import RetryPolicy

from va_workflows.activities.inputs import CallActivityInput

__all__ = [
    "HEARTBEAT_MISSING_AFTER_S",
    "HEARTBEAT_POLL_INTERVAL_S",
    "PRESENCE_RETRY",
    "PRESENCE_TIMEOUT",
    "WATCH_HEARTBEAT_TIMEOUT",
    "WATCH_RETRY",
    "WATCH_SLACK",
    "CallPresence",
    "WatchInput",
    "WatchResult",
    "watch_timeout",
]

HEARTBEAT_MISSING_AFTER_S: Final = 20
"""A heartbeat key absent this long means the media worker is gone (specs/01 §3)."""
HEARTBEAT_POLL_INTERVAL_S: Final = 3.0
"""How often the watch probes ``call:{<id>}:hb`` (the worker writes it every 3 s with a 10 s TTL)."""

WATCH_HEARTBEAT_TIMEOUT: Final = timedelta(seconds=30)
WATCH_RETRY: Final = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=0,
)
"""A crashed activity worker restarts the watch (unlimited attempts); ``CallWorkflow`` cancels it at the
end."""
WATCH_SLACK: Final = timedelta(seconds=600)
"""Start-to-close of the watch = the call's maximum duration plus this slack."""

PRESENCE_TIMEOUT: Final = timedelta(seconds=5)
PRESENCE_RETRY: Final = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=2),
    maximum_attempts=3,
)


def watch_timeout(max_duration_s: int) -> timedelta:
    """Start-to-close timeout of ``watch_call_heartbeat`` for a call of at most ``max_duration_s``."""
    return timedelta(seconds=max_duration_s) + WATCH_SLACK


class WatchInput(CallActivityInput):
    """``missing_after_s``: how long the heartbeat may be absent; ``poll_interval_s``: probe interval."""

    missing_after_s: int = Field(default=HEARTBEAT_MISSING_AFTER_S, gt=0)
    poll_interval_s: float = Field(default=HEARTBEAT_POLL_INTERVAL_S, gt=0)


class WatchResult(BaseModel):
    """The watch returns only when the heartbeat was missing for ``missing_s`` seconds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: Literal["heartbeat_lost"]
    missing_s: float


class CallPresence(BaseModel):
    """``owner``: a media worker holds ``call:{<id>}:owner``; ``heartbeat``: ``call:{<id>}:hb`` exists."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    owner: bool
    heartbeat: bool
