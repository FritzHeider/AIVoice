"""Workflow classes ``va_workflows`` provides for the worker's main task queue (specs/01-architecture.md §1).

``va_worker.activities.main_registrations`` builds the worker's main-queue registration from these and the
activity instances ``va_worker.activities.WorkerActivities`` constructs.
"""

from __future__ import annotations

from typing import Final

from va_workflows.calls import CallWorkflow

__all__ = ["CALL_WORKFLOWS"]

CALL_WORKFLOWS: Final[tuple[type, ...]] = (CallWorkflow,)
