"""Timers of ``CallWorkflow`` (specs/01-architecture.md §2.1 Decision P05-T06c, §3)."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

__all__ = [
    "DEFAULT_MAX_DURATION_S",
    "INBOUND_ANSWER_BACKSTOP",
    "MAX_DURATION_GRACE",
    "PIPELINE_GRACE",
    "REQUEUE_BUDGET",
    "REQUEUE_DELAY",
    "RING_BACKSTOP",
    "STREAM_FAILURE_GRACE",
]

REQUEUE_DELAY: Final = timedelta(seconds=15)
"""Wait between dial attempts while the org's concurrency cap is full."""
REQUEUE_BUDGET: Final = timedelta(minutes=30)
"""After this long of requeues the last attempt is final: a still full cap blocks the call."""
RING_BACKSTOP: Final = timedelta(seconds=300)
"""No answer and no hangup from the carrier this long after the dial (``dial_timeout_s`` is at most 120 s)."""
INBOUND_ANSWER_BACKSTOP: Final = timedelta(seconds=30)
"""No answer and no hangup from the carrier this long after the edge started an inbound call's workflow (the
edge answers within its 3 s accept budget, specs/06 §7 Decision P05-T12c)."""
MAX_DURATION_GRACE: Final = timedelta(seconds=120)
"""The backstop hangs up ``max_duration + 120 s`` after the answer (specs/01 §3)."""
STREAM_FAILURE_GRACE: Final = timedelta(seconds=5)
"""After ``streaming.failed`` the workflow waits this long before checking whether a worker owns the call."""
PIPELINE_GRACE: Final = timedelta(seconds=30)
"""After the carrier hangup the workflow waits this long for ``signal_pipeline_finished``."""
DEFAULT_MAX_DURATION_S: Final = 14_400
"""Backstop when the dial outcome carries no maximum duration (240 min, the ``max_duration`` maximum)."""
