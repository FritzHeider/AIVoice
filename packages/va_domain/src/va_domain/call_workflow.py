"""The ``CallWorkflow`` start contract shared by the api (starter) and the worker (specs/05 §5).

The api starts the workflow after the ``calls`` row is committed, with id ``call-<call_id>`` on the main task
queue and :class:`CallWorkflowInput` serialized as JSON (``model_dump(mode="json")``, the Temporal default
converter). The workflow re-reads everything else from the ``calls`` row.
"""

from __future__ import annotations

import uuid
from typing import Final

from pydantic import AwareDatetime, BaseModel, ConfigDict

from va_domain.carrier import call_workflow_id

__all__ = ["CALL_WORKFLOW_NAME", "CallWorkflowInput", "call_workflow_id"]

CALL_WORKFLOW_NAME: Final = "CallWorkflow"


class CallWorkflowInput(BaseModel):
    """What ``CallWorkflow`` is started with; ``scheduled_for`` is set for scheduled calls (UTC)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: uuid.UUID
    org_id: uuid.UUID
    scheduled_for: AwareDatetime | None = None

    def workflow_id(self) -> str:
        """``call-<call_id>``."""
        return call_workflow_id(self.call_id)
