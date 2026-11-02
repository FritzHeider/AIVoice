"""``CallWorkflow`` and its result (specs/01-architecture.md §2.1 Decision P05-T06c)."""

from va_workflows.calls.call_workflow import CallWorkflow
from va_workflows.calls.results import CallOutcome, CallWorkflowResult

__all__ = ["CallOutcome", "CallWorkflow", "CallWorkflowResult"]
