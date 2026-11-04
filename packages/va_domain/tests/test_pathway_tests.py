"""``PathwayTestWorkflow`` start contract, simulation jobs, and the run status rule (specs/12 §8, §9;
Decisions P07-T11c and P07-T11d)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from va_domain.pathway_tests import (
    DEFAULT_SIMULATION_TURNS,
    MAX_REGRESSION_SCENARIOS,
    MAX_RUN_TESTS,
    MAX_SIMULATIONS,
    REGRESSION_KEY,
    PathwayTestWorkflowInput,
    RegressionSettings,
    Scenario,
    SimulationJob,
    load_regression,
    pathway_test_workflow_id,
    regression_summary,
    run_status,
    simulation_jobs,
    with_regression,
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


_SCENARIO: dict[str, Any] = {"name": "Impatient", "persona": "In a hurry.", "goal": "Order placed"}


def test_regression_defaults_run_nothing_on_publish() -> None:
    settings = RegressionSettings()

    assert settings.model_dump(mode="json") == {
        "run_on_publish": False,
        "block_publish_on_failure": True,
        "include_simulations": False,
        "scenarios": [],
    }


@pytest.mark.parametrize(
    "raw",
    [
        {"run_on_publish": True, "extra": 1},
        {"scenarios": [_SCENARIO] * (MAX_REGRESSION_SCENARIOS + 1)},
        {"scenarios": [{**_SCENARIO, "max_turns": 0}]},
    ],
)
def test_regression_rejects_bad_settings(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RegressionSettings.model_validate(raw)


@pytest.mark.parametrize(
    "global_config",
    [{}, {REGRESSION_KEY: "x"}, {REGRESSION_KEY: {"run_on_publish": "maybe"}}, {REGRESSION_KEY: None}],
)
def test_load_regression_reads_missing_or_invalid_values_as_defaults(global_config: dict[str, Any]) -> None:
    assert load_regression(global_config) == RegressionSettings()


def test_load_regression_reads_a_stored_value() -> None:
    stored = {"globalPrompt": "Be kind.", REGRESSION_KEY: {"run_on_publish": True, "scenarios": [_SCENARIO]}}

    settings = load_regression(stored)

    assert settings.run_on_publish is True
    assert settings.block_publish_on_failure is True
    assert [scenario.name for scenario in settings.scenarios] == ["Impatient"]


def test_with_regression_copies_and_round_trips() -> None:
    original: dict[str, Any] = {"globalPrompt": "Be kind."}
    settings = RegressionSettings(include_simulations=True, scenarios=(Scenario.model_validate(_SCENARIO),))

    updated = with_regression(original, settings)

    assert original == {"globalPrompt": "Be kind."}
    assert updated["globalPrompt"] == "Be kind."
    assert load_regression(updated) == settings
    assert regression_summary(settings) == {
        "run_on_publish": False,
        "block_publish_on_failure": True,
        "include_simulations": True,
        "scenarios": 1,
    }
