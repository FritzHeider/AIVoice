"""Signal state of ``CallWorkflow``: what the edge, the stop API, and the media worker have reported.

Signal handlers only record (they run deterministically inside the workflow); the run loop acts on the flags.
Carrier events are deduplicated by ``telnyx_event_id`` and only primary-leg events count (specs/01 §2.1
Decision P05-T06c item 2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal

from va_domain.call_workflow import PipelineFinished
from va_domain.carrier import CarrierEvent

__all__ = ["FAX_RESULT", "CallSignals", "Recorded"]

type Recorded = Literal["recorded", "duplicate", "other_leg", "ignored"]

FAX_RESULT: Final = "fax_detected"
_ANSWERED: Final = "call.answered"
_HANGUP: Final = "call.hangup"
_PREMIUM_AMD_ENDED: Final = "call.machine.premium.detection.ended"
_STREAMING_FAILED: Final = "streaming.failed"


@dataclass(slots=True)
class CallSignals:
    """Flags set by signals; ``stream_failed_at`` is the workflow time of an unchecked
    ``streaming.failed``."""

    answered: bool = False
    hung_up: bool = False
    fax: bool = False
    cancel_requested: bool = False
    stream_failed_at: datetime | None = None
    pipeline_finished: PipelineFinished | None = None
    seen: set[str] = field(default_factory=set)

    def record(self, event: CarrierEvent, now: datetime) -> Recorded:
        """Apply one carrier event observed at workflow time ``now``."""
        if event.telnyx_event_id in self.seen:
            return "duplicate"
        self.seen.add(event.telnyx_event_id)
        if event.leg != "primary":
            return "other_leg"
        if event.type == _ANSWERED:
            self.answered = True
        elif event.type == _HANGUP:
            self.hung_up = True
        elif event.type == _PREMIUM_AMD_ENDED and event.payload.get("result") == FAX_RESULT:
            self.fax = True
        elif event.type == _STREAMING_FAILED and self.answered and self.stream_failed_at is None:
            self.stream_failed_at = now
        else:
            return "ignored"
        return "recorded"

    def request_cancel(self) -> bool:
        """Record a stop request; ``False`` (ignored) once the call is answered (specs/05 §7.2)."""
        if self.answered:
            return False
        self.cancel_requested = True
        return True

    def finish_pipeline(self, finished: PipelineFinished) -> None:
        """Record the first ``signal_pipeline_finished``."""
        if self.pipeline_finished is None:
            self.pipeline_finished = finished
