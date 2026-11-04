"""``PathwayTestWorkflow``: one unit-test or simulation run of a pathway version (specs/12-pathways-api.md
§8 and §9; Decisions P07-T11c and P07-T11d).

Started by the api (``POST /v1/pathway/{pathway_id}/tests/run`` or ``.../simulations``) with id
``pathway-test-run-<run_id>`` on the post-call queue. ``begin_pathway_test_run`` moves the run to
``running`` (a run that is gone or already finished ends the workflow). A unit run then runs every test as
its own ``run_pathway_unit_test`` activity, a simulation run every conversation as its own
``run_pathway_simulation`` activity, at most ``RUN_CONCURRENCY`` (8) at once; an item activity that fails
after its retries is stored as an ``error`` item (``runner_error``) by ``record_pathway_test_error`` /
``record_pathway_simulation_error``. ``finish_pathway_test_run`` orders the results and sets the final
status.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field
from temporalio import workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from va_domain.pathway_tests import (
        PATHWAY_TEST_WORKFLOW_NAME,
        RUN_CONCURRENCY,
        FinishedRunStatus,
        PathwayTestWorkflowInput,
        SimulationJob,
    )
    from va_workflows.activities.pathway_tests_inputs import (
        BOOKKEEPING_RETRY,
        BOOKKEEPING_TIMEOUT,
        SIMULATION_TIMEOUT,
        TEST_RETRY,
        TEST_TIMEOUT,
        FinishInput,
        FinishResult,
        RunRef,
        SimulationErrorInput,
        SimulationInput,
        UnitTestErrorInput,
        UnitTestInput,
    )

__all__ = ["PathwayTestRunResult", "PathwayTestWorkflow"]


class PathwayTestRunResult(BaseModel):
    """What the run did: ``started`` false when the run row was gone or finished before it began; ``status``
    the final status (``None`` when not started or the finish found the run gone); outcomes by test or
    simulation id."""

    model_config = ConfigDict(frozen=True)

    started: bool
    status: FinishedRunStatus | None = None
    outcomes: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Item:
    """One test or conversation of the run: its activity and the error recorder used after its retries."""

    key: str
    activity: str
    request: BaseModel
    timeout: timedelta
    error_activity: str
    error_request: BaseModel


@workflow.defn(name=PATHWAY_TEST_WORKFLOW_NAME)
class PathwayTestWorkflow:
    def __init__(self) -> None:
        self._outcomes: dict[str, str] = {}

    @workflow.query(name="pathway_test_outcomes")
    def pathway_test_outcomes(self) -> dict[str, str]:
        """Test or simulation id → outcome of the items finished so far."""
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
        items = [self._unit_item(start, test_id) for test_id in start.test_ids]
        items += [self._simulation_item(start, job) for job in start.simulations]
        await asyncio.gather(*(self._run_item(item, slots) for item in items))
        finished: FinishResult = await workflow.execute_activity(
            "finish_pathway_test_run",
            FinishInput(
                run_id=start.run_id,
                org_id=start.org_id,
                test_ids=start.test_ids,
                simulation_ids=tuple(job.simulation_id for job in start.simulations),
            ),
            start_to_close_timeout=BOOKKEEPING_TIMEOUT,
            retry_policy=BOOKKEEPING_RETRY,
            result_type=FinishResult,
        )
        return PathwayTestRunResult(started=True, status=finished.status, outcomes=dict(self._outcomes))

    @staticmethod
    def _unit_item(start: PathwayTestWorkflowInput, test_id: uuid.UUID) -> _Item:
        return _Item(
            key=str(test_id),
            activity="run_pathway_unit_test",
            request=UnitTestInput(
                run_id=start.run_id,
                org_id=start.org_id,
                pathway_id=start.pathway_id,
                version_number=start.version_number,
                test_id=test_id,
            ),
            timeout=TEST_TIMEOUT,
            error_activity="record_pathway_test_error",
            error_request=UnitTestErrorInput(run_id=start.run_id, org_id=start.org_id, test_id=test_id),
        )

    @staticmethod
    def _simulation_item(start: PathwayTestWorkflowInput, job: SimulationJob) -> _Item:
        return _Item(
            key=job.simulation_id,
            activity="run_pathway_simulation",
            request=SimulationInput(
                run_id=start.run_id,
                org_id=start.org_id,
                pathway_id=start.pathway_id,
                version_number=start.version_number,
                job=job,
            ),
            timeout=SIMULATION_TIMEOUT,
            error_activity="record_pathway_simulation_error",
            error_request=SimulationErrorInput(run_id=start.run_id, org_id=start.org_id, job=job),
        )

    async def _run_item(self, item: _Item, slots: asyncio.Semaphore) -> None:
        async with slots:
            try:
                outcome: str = await workflow.execute_activity(
                    item.activity,
                    item.request,
                    start_to_close_timeout=item.timeout,
                    retry_policy=TEST_RETRY,
                    result_type=str,
                )
            except ActivityError:
                workflow.logger.warning("pathway_test_run.item_failed key=%s", item.key)
                outcome = await self._record_error(item)
        self._outcomes[item.key] = outcome

    @staticmethod
    async def _record_error(item: _Item) -> str:
        """Store the ``runner_error`` item; ``unrecorded`` when that fails too (the finish then counts the
        item without a result as an error)."""
        try:
            recorded: str = await workflow.execute_activity(
                item.error_activity,
                item.error_request,
                start_to_close_timeout=BOOKKEEPING_TIMEOUT,
                retry_policy=BOOKKEEPING_RETRY,
                result_type=str,
            )
        except ActivityError:
            workflow.logger.warning("pathway_test_run.error_unrecorded key=%s", item.key)
            return "unrecorded"
        return recorded
