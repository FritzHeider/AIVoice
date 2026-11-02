"""Workflow classes ``va_workflows`` provides for the worker's task queues (specs/01-architecture.md §1).

``va_worker.activities.main_registrations`` builds the worker's registrations from these and the activity
instances ``va_worker.activities.WorkerActivities`` constructs.
"""

from __future__ import annotations

from typing import Final

from va_workflows.calls import CallWorkflow
from va_workflows.post_call import PostCallWorkflow

__all__ = ["CALL_WORKFLOWS", "POSTCALL_WORKFLOWS"]

CALL_WORKFLOWS: Final[tuple[type, ...]] = (CallWorkflow,)
"""The main queue (``TEMPORAL_TASK_QUEUE``)."""
POSTCALL_WORKFLOWS: Final[tuple[type, ...]] = (PostCallWorkflow,)
"""The post-call queue (specs/18 §1)."""
