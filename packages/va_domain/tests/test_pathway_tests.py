"""``PathwayTestWorkflow`` start contract, simulation jobs, and the run status rule (specs/12 §8, §9;
Decisions P07-T11c and P07-T11d)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from va_domain.pathway_tests import (
    DEFAULT_SIMULATION_TURNS,
    MAX_RUN_TESTS,
    MAX_SIMULATIONS,
    PathwayTestWorkflowInput,
    Scenario,
    SimulationJob,
    pathway_test_workflow_id,
    run_status,
    simulation_jobs,
)

RUN = uuid.UUID("0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d90")


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["passed", "passed"], "passed"),
        (["passed", "failed", "error"], "failed"),
        (["error", "passed"], "error"),
        (["passed", None], "error"),
        (["passed", "unexpected"], "error"),
        (["failed"], "failed"),
    ],
)
def test_run_status(statuses: list[str | None], expected: str) -> None:
    assert run_status(statuses) == expected


def test_the_workflow_id_names_the_run() -> None:
    start = PathwayTestWorkflowInput(
        run_id=RUN, org_id=uuid.uuid4(), pathway_id=uuid.uuid4(), version_number=2, test_ids=(uuid.uuid4(),)
    )
    assert start.workflow_id() == pathway_test_workflow_id(RUN) == f"pathway-test-run-{RUN}"
    assert PathwayTestWorkflowInput.model_validate(start.model_dump(mode="json")) == start


@pytest.mark.parametrize(
    ("version", "tests"), [(0, 1), (1, 0), (1, MAX_RUN_TESTS + 1)], ids=["version", "empty", "too_many"]
)
def test_inputs_are_validated(version: int, tests: int) -> None:
    with pytest.raises(ValidationError):
        PathwayTestWorkflowInput(
            run_id=RUN,
            org_id=RUN,
            pathway_id=RUN,
            version_number=version,
            test_ids=tuple(uuid.uuid4() for _ in range(tests)),
        )


def scenario(name: str = "Impatient caller", **fields: Any) -> Scenario:
    body = {"name": name, "persona": "You are impatient.", "goal": "Order placed", **fields}
    return Scenario.model_validate(body)


def test_simulation_jobs_run_scenario_order_then_run_order() -> None:
    first, second = scenario("A"), scenario("B")
    jobs = simulation_jobs([first, second], 3)
    assert [(job.simulation_id, job.scenario_index, job.run_index) for job in jobs] == [
        ("1-1", 1, 1),
        ("1-2", 1, 2),
        ("1-3", 1, 3),
        ("2-1", 2, 1),
        ("2-2", 2, 2),
        ("2-3", 2, 3),
    ]
    assert [job.scenario.name for job in jobs] == ["A", "A", "A", "B", "B", "B"]


def test_a_scenario_has_defaults_and_strips_its_texts() -> None:
    parsed = Scenario.model_validate(
        {"name": " Caller ", "persona": " You call. ", "goal": " Done ", "request_data": {"note": " keep "}}
    )
    assert (parsed.name, parsed.persona, parsed.goal) == ("Caller", "You call.", "Done")
    assert parsed.max_turns == DEFAULT_SIMULATION_TURNS
    assert parsed.request_data == {"note": " keep "}


@pytest.mark.parametrize(
    "fields",
    [{"persona": "   "}, {"max_turns": 0}, {"max_turns": 51}, {"goal": "x" * 2001}, {"extra": 1}],
    ids=["blank_persona", "no_turns", "too_many_turns", "long_goal", "unknown_key"],
)
def test_scenarios_are_validated(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        scenario(**fields)


def _start(**fields: object) -> PathwayTestWorkflowInput:
    return PathwayTestWorkflowInput.model_validate(
        {"run_id": RUN, "org_id": RUN, "pathway_id": RUN, "version_number": 1, **fields}
    )


def test_a_simulation_run_carries_jobs_and_round_trips() -> None:
    start = _start(kind="simulation", simulations=simulation_jobs([scenario()], 2))
    assert start.test_ids == ()
    assert PathwayTestWorkflowInput.model_validate(start.model_dump(mode="json")) == start


def _job(index: int) -> SimulationJob:
    return SimulationJob(simulation_id=f"1-{index}", scenario_index=1, run_index=index, scenario=scenario())


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "unit"},
        {"kind": "unit", "test_ids": [uuid.uuid4()], "simulations": [_job(1)]},
        {"kind": "simulation"},
        {"kind": "simulation", "simulations": [_job(1)], "test_ids": [uuid.uuid4()]},
        {"kind": "simulation", "simulations": [_job(i) for i in range(1, MAX_SIMULATIONS + 2)]},
    ],
    ids=["unit_no_tests", "unit_with_jobs", "simulation_no_jobs", "simulation_with_tests", "too_many"],
)
def test_the_work_must_match_the_kind(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _start(**fields)
