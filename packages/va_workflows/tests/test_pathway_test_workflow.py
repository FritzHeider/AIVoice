"""``PathwayTestWorkflow`` on the time-skipping server with fake activities: begin, at most 8 tests at once,
runner errors recorded per test, finish in test order (specs/12 §8; Decision P07-T11c)."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from workflow_rig import POSTCALL_QUEUE, workflow_env

from va_domain.pathway_tests import RUN_CONCURRENCY, PathwayTestWorkflowInput
from va_workflows.activities.pathway_tests_inputs import (
    FinishInput,
    FinishResult,
    RunRef,
    UnitTestErrorInput,
    UnitTestInput,
    UnitTestOutcome,
)
from va_workflows.pathway_tests import PathwayTestRunResult, PathwayTestWorkflow
from va_workflows.registration import POSTCALL_WORKFLOWS

pytestmark = pytest.mark.integration


@dataclass
class FakeTestActivities:
    """The four activities by their production names; ``outcomes`` scripts a status (or an exception) per
    test id (default ``passed``)."""

    begins: bool = True
    outcomes: dict[uuid.UUID, UnitTestOutcome | Exception] = field(default_factory=dict)
    record_fails: bool = False
    calls: list[tuple[str, Any]] = field(default_factory=list)
    in_flight: int = 0
    max_in_flight: int = 0
    full: asyncio.Event = field(default_factory=asyncio.Event)

    def activities(self) -> list[Callable[..., Any]]:
        @activity.defn(name="begin_pathway_test_run")
        async def begin(request: RunRef) -> bool:
            self.calls.append(("begin", request))
            return self.begins

        @activity.defn(name="run_pathway_unit_test")
        async def run_test(request: UnitTestInput) -> UnitTestOutcome:
            self.calls.append(("run", request.test_id))
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            if self.in_flight >= RUN_CONCURRENCY:
                self.full.set()
            try:
                # Hold until the cap is reached (bounded: a run with fewer tests never fills it).
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.full.wait(), timeout=0.5)
                await asyncio.sleep(0.01)
            finally:
                self.in_flight -= 1
            outcome = self.outcomes.get(request.test_id, "passed")
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        @activity.defn(name="record_pathway_test_error")
        async def record(request: UnitTestErrorInput) -> UnitTestOutcome:
            self.calls.append(("record", (request.test_id, request.error)))
            if self.record_fails:
                msg = "database down"
                raise ApplicationError(msg, non_retryable=True)
            return "error"

        @activity.defn(name="finish_pathway_test_run")
        async def finish(request: FinishInput) -> FinishResult:
            self.calls.append(("finish", request.test_ids))
            return FinishResult(status="failed" if "failed" in self.outcomes.values() else "passed")

        return [begin, run_test, record, finish]

    def named(self, name: str) -> list[Any]:
        return [value for call, value in self.calls if call == name]


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with workflow_env() as running:
        yield running


def start(tests: int) -> PathwayTestWorkflowInput:
    return PathwayTestWorkflowInput(
        run_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        pathway_id=uuid.uuid4(),
        version_number=3,
        test_ids=tuple(uuid.uuid4() for _ in range(tests)),
    )


async def run(env: WorkflowEnvironment, fakes: FakeTestActivities, begin: PathwayTestWorkflowInput) -> Any:
    async with Worker(
        env.client, task_queue=POSTCALL_QUEUE, workflows=[PathwayTestWorkflow], activities=fakes.activities()
    ):
        result: PathwayTestRunResult = await env.client.execute_workflow(
            PathwayTestWorkflow.run, begin, id=begin.workflow_id(), task_queue=POSTCALL_QUEUE
        )
    return result


def test_the_workflow_runs_on_the_post_call_queue() -> None:
    assert PathwayTestWorkflow in POSTCALL_WORKFLOWS


async def test_every_test_runs_at_most_eight_at_once_then_the_run_finishes_in_order(
    env: WorkflowEnvironment,
) -> None:
    fakes = FakeTestActivities()
    begin = start(20)
    failing = begin.test_ids[4]
    fakes.outcomes[failing] = "failed"
    result = await run(env, fakes, begin)
    assert (result.started, result.status) == (True, "failed")
    assert fakes.named("begin") == [RunRef(run_id=begin.run_id, org_id=begin.org_id)]
    assert sorted(fakes.named("run")) == sorted(begin.test_ids)
    assert fakes.max_in_flight == RUN_CONCURRENCY
    assert fakes.named("finish") == [begin.test_ids]
    assert [call for call, _ in fakes.calls][-1] == "finish"
    assert result.outcomes == {
        str(test_id): "failed" if test_id == failing else "passed" for test_id in begin.test_ids
    }


async def test_a_run_that_cannot_begin_runs_nothing(env: WorkflowEnvironment) -> None:
    fakes = FakeTestActivities(begins=False)
    result = await run(env, fakes, start(3))
    assert result == PathwayTestRunResult(started=False)
    assert [call for call, _ in fakes.calls] == ["begin"]


async def test_a_test_activity_that_fails_is_recorded_as_a_runner_error(env: WorkflowEnvironment) -> None:
    fakes = FakeTestActivities()
    begin = start(2)
    broken, fine = begin.test_ids
    fakes.outcomes[broken] = ApplicationError("model gateway exploded", non_retryable=True)
    result = await run(env, fakes, begin)
    assert fakes.named("record") == [(broken, "runner_error")]
    assert result.outcomes == {str(broken): "error", str(fine): "passed"}
    assert fakes.named("finish") == [begin.test_ids]


async def test_an_unrecordable_error_still_finishes_the_run(env: WorkflowEnvironment) -> None:
    fakes = FakeTestActivities(record_fails=True)
    begin = start(1)
    fakes.outcomes[begin.test_ids[0]] = ApplicationError("boom", non_retryable=True)
    result = await run(env, fakes, begin)
    assert result.outcomes == {str(begin.test_ids[0]): "unrecorded"}
    assert fakes.named("finish") == [begin.test_ids]
