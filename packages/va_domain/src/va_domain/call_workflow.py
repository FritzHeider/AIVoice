"""The ``CallWorkflow`` start contract shared by the api (starter) and the worker (specs/05 §5).

The api starts the workflow after the ``calls`` row is committed, with id ``call-<call_id>`` on the main task
queue and :class:`CallWorkflowInput` serialized as JSON (``model_dump(mode="json")``, the Temporal default
converter). The workflow re-reads everything else from the ``calls`` row.

The edge starts inbound calls the same way with ``direction="inbound"`` and the call's ``max_duration_s``
(specs/01 §2.1 Decision P05-T12c): the workflow skips the dial and waits for the edge's answer.

Signals (specs/01 §2.1 Decision P05-T06c): ``signal_carrier_event`` (:class:`~va_domain.carrier.CarrierEvent`,
from the edge), ``signal_cancel`` (no argument, from the stop API) and ``signal_pipeline_finished``
(:class:`PipelineFinished`, from the media worker, sent as ``model_dump(mode="json")``).
"""

from __future__ import annotations

import math
import uuid
from typing import Final, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

from va_domain.carrier import call_workflow_id

__all__ = [
    "CALL_WORKFLOW_NAME",
    "CANCEL_SIGNAL",
    "PIPELINE_FINISHED_SIGNAL",
    "CallWorkflowInput",
    "PipelineFinished",
    "call_workflow_id",
    "max_duration_seconds",
]

CALL_WORKFLOW_NAME: Final = "CallWorkflow"
CANCEL_SIGNAL: Final = "signal_cancel"
"""Stop a call that is not answered yet (specs/05 §7.2); ignored once the call is answered."""
PIPELINE_FINISHED_SIGNAL: Final = "signal_pipeline_finished"
"""The media worker ended the conversation and recorded the end (specs/01 §2.1 step 8)."""


def max_duration_seconds(max_duration_min: float) -> int:
    """``max_duration`` (minutes) in whole seconds, rounded up."""
    return math.ceil(max_duration_min * 60)


class CallWorkflowInput(BaseModel):
    """What ``CallWorkflow`` is started with.

    ``scheduled_for`` is set for scheduled outbound calls (UTC). Inbound calls (``direction="inbound"``)
    carry ``max_duration_s`` (the in-call backstop; an outbound call learns it from the dial) and are never
    scheduled.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: uuid.UUID
    org_id: uuid.UUID
    scheduled_for: AwareDatetime | None = None
    direction: Literal["outbound", "inbound"] = "outbound"
    max_duration_s: int | None = None

    @model_validator(mode="after")
    def _direction_fields(self) -> Self:
        if self.direction == "inbound":
            if self.max_duration_s is None:
                msg = "an inbound call needs max_duration_s"
                raise ValueError(msg)
            if self.scheduled_for is not None:
                msg = "an inbound call cannot be scheduled"
                raise ValueError(msg)
        elif self.max_duration_s is not None:
            msg = "max_duration_s is for inbound calls only"
            raise ValueError(msg)
        return self

    def workflow_id(self) -> str:
        """``call-<call_id>``."""
        return call_workflow_id(self.call_id)


class PipelineFinished(BaseModel):
    """The argument of ``signal_pipeline_finished``: the ``end_reason``/``ended_by`` the worker recorded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    end_reason: str
    ended_by: str
