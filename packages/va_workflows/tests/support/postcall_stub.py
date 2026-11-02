"""A stand-in ``PostCallWorkflow`` for ``CallWorkflow`` tests: it returns its input so tests can read it.

Kept in its own module because the workflow sandbox re-imports the module that defines a workflow.
"""

from __future__ import annotations

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from va_domain.post_call import POSTCALL_WORKFLOW_NAME, PostCallWorkflowInput

__all__ = ["StubPostCall"]


@workflow.defn(name=POSTCALL_WORKFLOW_NAME)
class StubPostCall:
    @workflow.run
    async def run(self, request: PostCallWorkflowInput) -> PostCallWorkflowInput:
        return request
