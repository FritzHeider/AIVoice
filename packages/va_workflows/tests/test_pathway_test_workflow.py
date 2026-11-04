"""``PathwayTestWorkflow`` on the time-skipping server with fake activities: begin, at most 8 tests or
conversations at once, runner errors recorded per item, finish in result order (specs/12 §8, §9; Decisions
P07-T11c and P07-T11d)."""

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

from va_domain.pathway_tests import (
    RUN_CONCURRENCY,
    PathwayTestWorkflowInput,
    PublishActor,
    RegressionPublish,
    Scenario,
    simulation_jobs,
)
from va_workflows.activities.pathway_tests_inputs import (
    FinishInput,
    FinishResult,
    RunRef,
    SettleInput,
    SettleResult,
    SimulationErrorInput,
    SimulationInput,
    UnitTestErrorInput,
    UnitTestInput,
    UnitTestOutcome,
)
from va_workflows.pathway_tests import PathwayTestRunResult, PathwayTestWorkflow
from va_workflows.registration import POSTCALL_WORKFLOWS

pytestmark = pytest.mark.integration


@dataclass
class FakeTestActivities:
    """The seven activities by their production names; ``outcomes`` scripts a status (or an exception) per
    test or simulation id (default ``passed``); ``finish_status`` overrides the finish result (``"gone"`` =
    the run row vanished); ``settles`` fails the settle activity when it is an exception."""

    begins: bool = True
    finish_status: str | None = None
    settles: Exception | None = None
    outcomes: dict[uuid.UUID | str, UnitTestOutcome | Exception] = field(default_factory=dict)
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
            return await self._hold(request.test_id)

        @activity.defn(name="run_pathway_simulation")
        async def run_simulation(request: SimulationInput) -> UnitTestOutcome:
            self.calls.append(("simulate", (request.job.simulation_id, request.version_number)))
            return await self._hold(request.job.simulation_id)

        @activity.defn(name="record_pathway_test_error")
        async def record(request: UnitTestErrorInput) -> UnitTestOutcome:
            self.calls.append(("record", (request.test_id, request.error)))
            return self._recorded()

        @activity.defn(name="record_pathway_simulation_error")
        async def record_simulation(request: SimulationErrorInput) -> UnitTestOutcome:
            self.calls.append(("record_simulation", (request.job.simulation_id, request.error)))
            return self._recorded()

        @activity.defn(name="finish_pathway_test_run")
        async def finish(request: FinishInput) -> FinishResult:
            self.calls.append(("finish_simulations", request.simulation_ids))
            self.calls.append(("finish", request.test_ids))
            if self.finish_status == "gone":
                return FinishResult(status=None)
            if self.finish_status is not None:
                return FinishResult.model_validate({"status": self.finish_status})
            return FinishResult(status="failed" if "failed" in self.outcomes.values() else "passed")

        @activity.defn(name="settle_regression_publish")
        async def settle(request: SettleInput) -> SettleResult:
            self.calls.append(("settle", request))
            if self.settles is not None:
                raise self.settles
            return SettleResult(outcome="blocked" if "failed" in self.outcomes.values() else "published")

        return [begin, run_test, run_simulation, record, record_simulation, finish, settle]

    def _recorded(self) -> UnitTestOutcome:
        if self.record_fails:
            msg = "database down"
            raise ApplicationError(msg, non_retryable=True)
        return "error"

    async def _hold(self, key: uuid.UUID | str) -> UnitTestOutcome:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        if self.in_flight >= RUN_CONCURRENCY:
            self.full.set()
        try:
            # Hold until the cap is reached (bounded: a run with fewer items never fills it).
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.full.wait(), timeout=0.5)
            await asyncio.sleep(0.01)
        finally:
            self.in_flight -= 1
        outcome = self.outcomes.get(key, "passed")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

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


def simulation_start(scenarios: int, runs: int) -> PathwayTestWorkflowInput:
    scenario = Scenario(name="Caller", persona="You call.", goal="Done")
    return PathwayTestWorkflowInput(
        run_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        pathway_id=uuid.uuid4(),
        version_number=4,
        kind="simulation",
        simulations=simulation_jobs([scenario] * scenarios, runs),
    )


async def test_a_simulation_run_runs_every_conversation_eight_at_once(env: WorkflowEnvironment) -> None:
    fakes = FakeTestActivities()
    begin = simulation_start(4, 3)
    fakes.outcomes["2-2"] = "failed"
    result = await run(env, fakes, begin)
    ids = [job.simulation_id for job in begin.simulations]
    assert (result.started, result.status) == (True, "failed")
    assert sorted(fakes.named("simulate")) == sorted((simulation_id, 4) for simulation_id in ids)
    assert fakes.named("run") == []
    assert fakes.max_in_flight == RUN_CONCURRENCY
    assert fakes.named("finish") == [()]
    assert fakes.named("finish_simulations") == [tuple(ids)]
    assert result.outcomes == {
        simulation_id: "failed" if simulation_id == "2-2" else "passed" for simulation_id in ids
    }


async def test_a_failing_simulation_activity_is_recorded_as_a_runner_error(env: WorkflowEnvironment) -> None:
    fakes = FakeTestActivities()
    begin = simulation_start(1, 2)
    fakes.outcomes["1-1"] = ApplicationError("caller model exploded", non_retryable=True)
    result = await run(env, fakes, begin)
    assert fakes.named("record_simulation") == [("1-1", "runner_error")]
    assert fakes.named("record") == []
    assert result.outcomes == {"1-1": "error", "1-2": "passed"}


def regression_start(*, tests: int, scenarios: int = 0, publish: bool = True) -> PathwayTestWorkflowInput:
    org_id = uuid.uuid4()
    intent = RegressionPublish(
        environment="staging",
        version_number=3,
        graph_hash="a" * 64,
        actor=PublishActor(org_id=org_id, actor_type="user", actor_id=uuid.uuid4()),
    )
    return PathwayTestWorkflowInput(
        run_id=uuid.uuid4(),
        org_id=org_id,
        pathway_id=uuid.uuid4(),
        version_number=3,
        kind="regression",
        test_ids=tuple(uuid.uuid4() for _ in range(tests)),
        simulations=simulation_jobs(
            [Scenario(name=f"S{i}", persona="p", goal="g") for i in range(scenarios)], 1
        ),
        publish=intent if publish else None,
    )


async def test_a_passed_blocking_regression_run_settles_its_publish(env: WorkflowEnvironment) -> None:
    fakes = FakeTestActivities()
    begin = regression_start(tests=2, scenarios=1)
    result = await run(env, fakes, begin)
    assert (result.status, result.publish) == ("passed", "published")
    assert len(fakes.named("run")) == 2
    assert [key for key, _ in fakes.named("simulate")] == ["1-1"]
    (settle,) = fakes.named("settle")
    assert begin.publish is not None
    assert settle == SettleInput(
        run_id=begin.run_id, org_id=begin.org_id, pathway_id=begin.pathway_id, publish=begin.publish
    )


async def test_a_failed_regression_run_settles_as_blocked(env: WorkflowEnvironment) -> None:
    begin = regression_start(tests=2)
    fakes = FakeTestActivities(outcomes={begin.test_ids[1]: "failed"})
    result = await run(env, fakes, begin)
    assert (result.status, result.publish) == ("failed", "blocked")
    assert len(fakes.named("settle")) == 1


async def test_a_reporting_regression_run_settles_nothing(env: WorkflowEnvironment) -> None:
    fakes = FakeTestActivities()
    result = await run(env, fakes, regression_start(tests=1, publish=False))
    assert (result.status, result.publish) == ("passed", None)
    assert fakes.named("settle") == []


@pytest.mark.parametrize(("begins", "finish_status"), [(False, None), (True, "gone")])
async def test_a_run_that_never_finished_settles_nothing(
    env: WorkflowEnvironment, begins: bool, finish_status: str | None
) -> None:
    fakes = FakeTestActivities(begins=begins, finish_status=finish_status)
    result = await run(env, fakes, regression_start(tests=1))
    assert (result.status, result.publish) == (None, None)
    assert fakes.named("settle") == []


async def test_a_settle_that_keeps_failing_leaves_the_publish_unsettled(env: WorkflowEnvironment) -> None:
    fakes = FakeTestActivities(settles=ApplicationError("database down", non_retryable=True))
    result = await run(env, fakes, regression_start(tests=1))
    assert (result.status, result.publish) == ("passed", "unsettled")
