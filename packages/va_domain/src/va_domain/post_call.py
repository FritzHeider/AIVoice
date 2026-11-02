"""The ``PostCallWorkflow`` start contract shared by ``CallWorkflow`` (parent) and the post-call worker
(specs/18-post-call-analysis.md §1, Decision P05-T06c).

``CallWorkflow`` starts the child with id ``postcall-<call_id>`` on the post-call task queue, which is
derived from the main queue by replacing its ``-main`` suffix (``va-main`` → ``va-postcall``), the same rule
the worker uses to poll its queues.
"""

from __future__ import annotations

import uuid
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "POSTCALL_BUDGET_S",
    "POSTCALL_STEPS",
    "POSTCALL_STEPS_KEY",
    "POSTCALL_WORKFLOW_NAME",
    "RECORDING_SAVED_SIGNAL",
    "RECORDING_WAIT_S",
    "PostCallStep",
    "PostCallTimings",
    "PostCallWorkflowInput",
    "postcall_task_queue",
    "postcall_workflow_id",
]

POSTCALL_WORKFLOW_NAME: Final = "PostCallWorkflow"
RECORDING_SAVED_SIGNAL: Final = "recording_saved"
"""Signal the edge sends ``postcall-<id>`` for ``call.recording.saved`` (the JSON ``CarrierEvent``;
specs/18 §1 step 2, specs/06 §11 Decision P05-T14)."""
type PostCallStep = Literal[
    "finalize_call_record",
    "await_recording",
    "store_recording",
    "retranscribe",
    "summarize",
    "classify_disposition",
    "extract_analysis_schema",
    "extract_citations",
    "run_post_call_evals",
    "update_memory",
    "finalize_billing",
    "sentiment",
    "emit_completed",
]
"""The specs/18 §1 step names (the table's activity column)."""

POSTCALL_STEPS: Final[tuple[PostCallStep, ...]] = (
    "finalize_call_record",
    "await_recording",
    "store_recording",
    "retranscribe",
    "summarize",
    "classify_disposition",
    "extract_analysis_schema",
    "extract_citations",
    "run_post_call_evals",
    "update_memory",
    "finalize_billing",
    "sentiment",
    "emit_completed",
)
"""Every step in table order (step ``n`` is ``POSTCALL_STEPS[n - 1]``)."""

POSTCALL_STEPS_KEY: Final = "postcall_steps"
"""``calls.config`` key holding the JSON array of completed step names (not a call parameter)."""
RECORDING_WAIT_S: Final = 600
"""How long ``await_recording`` waits for the ``recording_saved`` signal (10 min)."""
POSTCALL_BUDGET_S: Final = 1800
"""The whole workflow's budget before ``emit_completed`` runs regardless (30 min)."""
_MAIN_SUFFIX: Final = "-main"
_POSTCALL_SUFFIX: Final = "-postcall"


class PostCallTimings(BaseModel):
    """The workflow's waits; production uses the defaults (tests shorten the budget)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    recording_wait_s: int = Field(default=RECORDING_WAIT_S, gt=0)
    budget_s: int = Field(default=POSTCALL_BUDGET_S, gt=0)


class PostCallWorkflowInput(BaseModel):
    """What ``PostCallWorkflow`` is started with; it re-reads everything else from the ``calls`` row.

    ``timings`` is never set by ``CallWorkflow`` (``None`` = the spec values)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: uuid.UUID
    org_id: uuid.UUID
    timings: PostCallTimings | None = None


def postcall_workflow_id(call_id: uuid.UUID) -> str:
    """``postcall-<id>``."""
    return f"postcall-{call_id}"


def postcall_task_queue(main_queue: str) -> str:
    """``va-main`` → ``va-postcall``; a queue without the ``-main`` suffix is a configuration error."""
    if not main_queue.endswith(_MAIN_SUFFIX) or main_queue == _MAIN_SUFFIX:
        msg = f"the main task queue must end with {_MAIN_SUFFIX!r}, got {main_queue!r}"
        raise ValueError(msg)
    return main_queue.removesuffix(_MAIN_SUFFIX) + _POSTCALL_SUFFIX
