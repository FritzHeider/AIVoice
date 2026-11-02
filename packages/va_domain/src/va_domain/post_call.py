"""The ``PostCallWorkflow`` start contract shared by ``CallWorkflow`` (parent) and the post-call worker
(specs/18-post-call-analysis.md §1, Decision P05-T06c).

``CallWorkflow`` starts the child with id ``postcall-<call_id>`` on the post-call task queue, which is
derived from the main queue by replacing its ``-main`` suffix (``va-main`` → ``va-postcall``), the same rule
the worker uses to poll its queues.
"""

from __future__ import annotations

import uuid
from typing import Final

from pydantic import BaseModel, ConfigDict

__all__ = [
    "POSTCALL_WORKFLOW_NAME",
    "RECORDING_SAVED_SIGNAL",
    "PostCallWorkflowInput",
    "postcall_task_queue",
    "postcall_workflow_id",
]

POSTCALL_WORKFLOW_NAME: Final = "PostCallWorkflow"
RECORDING_SAVED_SIGNAL: Final = "recording_saved"
"""Signal the edge sends ``postcall-<id>`` for ``call.recording.saved`` (the JSON ``CarrierEvent``;
specs/18 §1 step 2, specs/06 §11 Decision P05-T14)."""
_MAIN_SUFFIX: Final = "-main"
_POSTCALL_SUFFIX: Final = "-postcall"


class PostCallWorkflowInput(BaseModel):
    """What ``PostCallWorkflow`` is started with; it re-reads everything else from the ``calls`` row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: uuid.UUID
    org_id: uuid.UUID


def postcall_workflow_id(call_id: uuid.UUID) -> str:
    """``postcall-<id>``."""
    return f"postcall-{call_id}"


def postcall_task_queue(main_queue: str) -> str:
    """``va-main`` → ``va-postcall``; a queue without the ``-main`` suffix is a configuration error."""
    if not main_queue.endswith(_MAIN_SUFFIX) or main_queue == _MAIN_SUFFIX:
        msg = f"the main task queue must end with {_MAIN_SUFFIX!r}, got {main_queue!r}"
        raise ValueError(msg)
    return main_queue.removesuffix(_MAIN_SUFFIX) + _POSTCALL_SUFFIX
