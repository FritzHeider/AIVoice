"""``PathwayTestWorkflow``: one unit-test run of a pathway version (specs/12-pathways-api.md §8; Decision
P07-T11c).

Started by the api (``POST /v1/pathway/{pathway_id}/tests/run``) with id ``pathway-test-run-<run_id>`` on
the post-call queue. ``begin_pathway_test_run`` moves the run to ``running`` (a run that is gone or already
finished ends the workflow), then every test runs as its own ``run_pathway_unit_test`` activity, at most
``RUN_CONCURRENCY`` (8) at once; a test activity that fails after its retries is stored as an ``error`` item
(``runner_error``) by ``record_pathway_test_error``. ``finish_pathway_test_run`` orders the results and
sets the final status.
"""

from __future__ import annotations

import asyncio
import uuid

from pydantic import BaseModel, ConfigDict, Field
from temporalio import workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from va_domain.pathway_tests import (
        PATHWAY_TEST_WORKFLOW_NAME,
        RUN_CONCURRENCY,
        FinishedRunStatus,
        PathwayTestWorkflowInput,
    )
    from va_workflows.activities.pathway_tests_inputs import (
        BOOKKEEPING_RETRY,
        BOOKKEEPING_TIMEOUT,
        TEST_RETRY,
        TEST_TIMEOUT,
        FinishInput,
        FinishResult,
        RunRef,
        UnitTestErrorInput,
        UnitTestInput,
    )

__all__ = ["PathwayTestRunResult", "PathwayTestWorkflow"]


class PathwayTestRunResult(BaseModel):
    """What the run did: ``started`` false when the run row was gone or finished before it began; ``status``
    the final status (``None`` when not started or the finish found the run gone); per-test outcomes."""

    model_config = ConfigDict(frozen=True)

    started: bool
    status: FinishedRunStatus | None = None
    outcomes: dict[str, str] = Field(default_factory=dict)


@workflow.defn(name=PATHWAY_TEST_WORKFLOW_NAME)
class PathwayTestWorkflow:
    def __init__(self) -> None:
        self._outcomes: dict[str, str] = {}

    @workflow.query(name="pathway_test_outcomes")
    def pathway_test_outcomes(self) -> dict[str, str]:
        """Test id → outcome of the tests finished so far."""
        return dict(self._outcomes)

    @workflow.run
    async def run(self, start: PathwayTestWorkflowInput) -> PathwayTestRunResult:
        ref = RunRef(run_id=start.run_id, org_id=start.org_id)
        began: bool = await workflow.execute_activity(
            "begin_pathway_test_run",
            ref,
            start_to_close_timeout=BOOKKEEPING_TIMEOUT,
            retry_policy=BOOKKEEPING_RETRY,
            result_type=bool,
        )
        if not began:
            workflow.logger.warning("pathway_test_run.not_runnable")
            return PathwayTestRunResult(started=False)
        slots = asyncio.Semaphore(RUN_CONCURRENCY)
        await asyncio.gather(*(self._run_test(start, test_id, slots) for test_id in start.test_ids))
        finished: FinishResult = await workflow.execute_activity(
            "finish_pathway_test_run",
            FinishInput(run_id=start.run_id, org_id=start.org_id, test_ids=start.test_ids),
            start_to_close_timeout=BOOKKEEPING_TIMEOUT,
            retry_policy=BOOKKEEPING_RETRY,
            result_type=FinishResult,
        )
        return PathwayTestRunResult(started=True, status=finished.status, outcomes=dict(self._outcomes))

    async def _run_test(
        self, start: PathwayTestWorkflowInput, test_id: uuid.UUID, slots: asyncio.Semaphore
    ) -> None:
        async with slots:
            try:
                outcome: str = await workflow.execute_activity(
                    "run_pathway_unit_test",
                    UnitTestInput(
                        run_id=start.run_id,
                        org_id=start.org_id,
                        pathway_id=start.pathway_id,
                        version_number=start.version_number,
                        test_id=test_id,
                    ),
                    start_to_close_timeout=TEST_TIMEOUT,
                    retry_policy=TEST_RETRY,
                    result_type=str,
                )
            except ActivityError:
                workflow.logger.warning("pathway_test_run.test_failed test_id=%s", test_id)
                outcome = await self._record_error(start, test_id)
        self._outcomes[str(test_id)] = outcome

    @staticmethod
    async def _record_error(start: PathwayTestWorkflowInput, test_id: uuid.UUID) -> str:
        """Store the ``runner_error`` item; ``unrecorded`` when that fails too (the finish then counts the
        test without a result as an error)."""
        try:
            recorded: str = await workflow.execute_activity(
                "record_pathway_test_error",
                UnitTestErrorInput(run_id=start.run_id, org_id=start.org_id, test_id=test_id),
                start_to_close_timeout=BOOKKEEPING_TIMEOUT,
                retry_policy=BOOKKEEPING_RETRY,
                result_type=str,
            )
        except ActivityError:
            workflow.logger.warning("pathway_test_run.error_unrecorded test_id=%s", test_id)
            return "unrecorded"
        return recorded
