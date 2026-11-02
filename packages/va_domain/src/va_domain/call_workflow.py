"""The ``CallWorkflow`` start contract shared by the api (starter) and the worker (specs/05 §5).

The api starts the workflow after the ``calls`` row is committed, with id ``call-<call_id>`` on the main task
queue and :class:`CallWorkflowInput` serialized as JSON (``model_dump(mode="json")``, the Temporal default
converter). The workflow re-reads everything else from the ``calls`` row.

Signals (specs/01 §2.1 Decision P05-T06c): ``signal_carrier_event`` (:class:`~va_domain.carrier.CarrierEvent`,
from the edge), ``signal_cancel`` (no argument, from the stop API) and ``signal_pipeline_finished``
(:class:`PipelineFinished`, from the media worker, sent as ``model_dump(mode="json")``).
"""

from __future__ import annotations

import uuid
from typing import Final

from pydantic import AwareDatetime, BaseModel, ConfigDict

from va_domain.carrier import call_workflow_id

__all__ = [
    "CALL_WORKFLOW_NAME",
    "CANCEL_SIGNAL",
    "PIPELINE_FINISHED_SIGNAL",
    "CallWorkflowInput",
    "PipelineFinished",
    "call_workflow_id",
]

CALL_WORKFLOW_NAME: Final = "CallWorkflow"
CANCEL_SIGNAL: Final = "signal_cancel"
"""Stop a call that is not answered yet (specs/05 §7.2); ignored once the call is answered."""
PIPELINE_FINISHED_SIGNAL: Final = "signal_pipeline_finished"
"""The media worker ended the conversation and recorded the end (specs/01 §2.1 step 8)."""


class CallWorkflowInput(BaseModel):
    """What ``CallWorkflow`` is started with; ``scheduled_for`` is set for scheduled calls (UTC)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: uuid.UUID
    org_id: uuid.UUID
    scheduled_for: AwareDatetime | None = None

    def workflow_id(self) -> str:
        """``call-<call_id>``."""
        return call_workflow_id(self.call_id)


class PipelineFinished(BaseModel):
    """The argument of ``signal_pipeline_finished``: the ``end_reason``/``ended_by`` the worker recorded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    end_reason: str
    ended_by: str
