"""A workflow that runs one activity under ``STORE_RECORDING_RETRY``, for the busy-retry delay test.

Kept in its own module because the workflow sandbox re-imports the module that defines a workflow.
"""

from __future__ import annotations

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from va_workflows.activities.recording_inputs import (
        STORE_RECORDING_RETRY,
        STORE_RECORDING_TIMEOUT,
    )

__all__ = ["BUSY_ONCE_ACTIVITY", "BusyRetryStub"]

BUSY_ONCE_ACTIVITY = "busy_once"


@workflow.defn(name="BusyRetryStub")
class BusyRetryStub:
    @workflow.run
    async def run(self) -> tuple[int, float]:
        """The attempt that succeeded and the workflow time the activity took (retry waits included)."""
        started = workflow.now()
        attempts: int = await workflow.execute_activity(
            BUSY_ONCE_ACTIVITY,
            start_to_close_timeout=STORE_RECORDING_TIMEOUT,
            retry_policy=STORE_RECORDING_RETRY,
            result_type=int,
        )
        return attempts, (workflow.now() - started).total_seconds()
