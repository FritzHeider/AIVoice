"""Workflow classes ``va_workflows`` provides for the worker's main task queue (specs/01-architecture.md §1).

The worker builds its :class:`~va_worker.task_queues.TaskQueueRegistration` from these and the activity
instances it constructs (P05-T06d).
"""

from __future__ import annotations

from typing import Final

from va_workflows.calls import CallWorkflow

__all__ = ["CALL_WORKFLOWS"]

CALL_WORKFLOWS: Final[tuple[type, ...]] = (CallWorkflow,)
