"""The result of ``PostCallWorkflow`` (specs/18-post-call-analysis.md §1, Decision P05-T18b)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from va_domain.post_call import PostCallStep
from va_workflows.activities.post_call_inputs import CompletedStatus

__all__ = ["PostCallResult", "PostCallStatus", "RecordingOutcome"]

type PostCallStatus = Literal["completed", "call_not_found"]
"""``completed``: the workflow ran to its end (individual steps may have failed); ``call_not_found``: the
call does not exist, nothing after step 1 ran."""

type RecordingOutcome = Literal[
    "not_wanted", "no_storage", "stored", "already_stored", "failed", "missing", "skipped"
]
"""``not_wanted``: no recording asked for (or the call never connected); ``no_storage``: wanted but the worker
has no object storage; ``stored``/``already_stored``/``failed``: the ``store_recording`` outcome;
``missing``: no ``recording_saved`` within the wait (``RECORDING_MISSING`` recorded); ``skipped``: an
earlier run completed the steps, or the recording step failed before storing."""


class PostCallResult(BaseModel):
    """What the run did: ``ran`` lists the steps executed (in order), ``skipped`` the planned steps not run,
    ``failed`` the steps that failed after their retries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: PostCallStatus
    ran: tuple[PostCallStep, ...] = ()
    skipped: tuple[PostCallStep, ...] = ()
    failed: tuple[PostCallStep, ...] = ()
    recording: RecordingOutcome = "not_wanted"
    price_micros: int | None = None
    timed_out: bool = False
    completed: CompletedStatus | None = None
