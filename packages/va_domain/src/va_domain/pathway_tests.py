"""The ``PathwayTestWorkflow`` start contract shared by the api (starter) and the worker
(specs/12-pathways-api.md §8 and §9; Decisions P07-T11c and P07-T11d there).

The api inserts a ``pathway_test_runs`` row and starts the workflow with id ``pathway-test-run-<run_id>`` on
the post-call task queue (``va_domain.post_call.postcall_task_queue``). Workflow code imports this module, so
it imports only pydantic.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Annotated, Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    model_validator,
)

__all__ = [
    "DEFAULT_SIMULATION_TURNS",
    "MAX_REGRESSION_SCENARIOS",
    "MAX_RUNS_PER_SCENARIO",
    "MAX_RUN_TESTS",
    "MAX_SCENARIOS",
    "MAX_SIMULATIONS",
    "MAX_SIMULATION_TURNS",
    "PATHWAY_TEST_WORKFLOW_NAME",
    "REGRESSION_KEY",
    "RUN_CONCURRENCY",
    "FinishedRunStatus",
    "PathwayTestWorkflowInput",
    "PublishActor",
    "PublishOutcome",
    "RegressionPublish",
    "RegressionSettings",
    "RunKind",
    "Scenario",
    "SimulationJob",
    "load_regression",
    "pathway_test_workflow_id",
    "regression_enabled",
    "regression_jobs",
    "regression_summary",
    "run_status",
    "simulation_jobs",
    "with_regression",
]

PATHWAY_TEST_WORKFLOW_NAME: Final = "PathwayTestWorkflow"
RUN_CONCURRENCY: Final = 8
"""Tests of one run in flight at once (specs/12 §8)."""
MAX_RUN_TESTS: Final = 500
"""Tests one run may hold."""
MAX_SCENARIOS: Final = 20
"""Scenarios one simulation request may hold."""
MAX_RUNS_PER_SCENARIO: Final = 10
"""Conversations per scenario."""
MAX_SIMULATIONS: Final = MAX_SCENARIOS * MAX_RUNS_PER_SCENARIO
"""Conversations one simulation run may hold."""
MAX_SIMULATION_TURNS: Final = 50
"""The highest ``max_turns`` of a scenario."""
DEFAULT_SIMULATION_TURNS: Final = 20
MAX_REGRESSION_SCENARIOS: Final = 20
"""Scenarios a regression suite may hold."""
REGRESSION_KEY: Final = "regression"
"""The draft ``global_config`` key holding the regression settings (specs/12 §9)."""

type RunKind = Literal["unit", "simulation", "regression"]
"""The run kinds ``PathwayTestWorkflow`` executes (a ``regression`` run holds unit tests, simulations, or
both)."""

type FinishedRunStatus = Literal["passed", "failed", "error"]

type PublishOutcome = Literal["pending", "published", "blocked", "stale", "version_unavailable"]
"""What became of the publish attached to a regression run (specs/12 §4; Decision P07-T11e2)."""


def pathway_test_workflow_id(run_id: uuid.UUID) -> str:
    """``pathway-test-run-<run_id>``."""
    return f"pathway-test-run-{run_id}"


class Scenario(BaseModel):
    """One simulated caller: who they are (``persona``), what the conversation should achieve (``goal``), the
    caller-turn cap, and the request data the pathway sees."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    persona: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    goal: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    max_turns: int = Field(default=DEFAULT_SIMULATION_TURNS, ge=1, le=MAX_SIMULATION_TURNS)
    request_data: dict[str, JsonValue] = Field(default_factory=dict)


class SimulationJob(BaseModel):
    """One conversation of a simulation run: run ``run_index`` of scenario ``scenario_index`` (both
    1-based); ``simulation_id`` is ``"<scenario_index>-<run_index>"``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    simulation_id: str
    scenario_index: int = Field(ge=1)
    run_index: int = Field(ge=1)
    scenario: Scenario


def simulation_jobs(scenarios: Iterable[Scenario], runs_per_scenario: int) -> tuple[SimulationJob, ...]:
    """Every conversation of a simulation run in result order: scenario order, then run order."""
    return tuple(
        SimulationJob(
            simulation_id=f"{scenario_index}-{run_index}",
            scenario_index=scenario_index,
            run_index=run_index,
            scenario=scenario,
        )
        for scenario_index, scenario in enumerate(scenarios, start=1)
        for run_index in range(1, runs_per_scenario + 1)
    )


class PublishActor(BaseModel):
    """Who asked for a publish (the ``va_db.audit.AuditActor`` fields): the worker writes the
    ``pathway.publish`` audit row in their name. Carried only in the workflow input, never in the run row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: uuid.UUID
    actor_type: Literal["user", "api_key", "system"]
    actor_id: uuid.UUID | None
    ip: str | None = None
    user_agent: str | None = None
    acting_parent_org_id: uuid.UUID | None = None
    platform_admin: bool = False


class RegressionPublish(BaseModel):
    """The publish a blocking regression run completes when it passes: the version, its environment, the
    ``graph_hash`` the api validated (a version whose graph changed since is not published), and who asked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: Literal["production", "staging"]
    version_number: int = Field(ge=1)
    graph_hash: str
    published_by: uuid.UUID | None = None
    actor: PublishActor


class PathwayTestWorkflowInput(BaseModel):
    """One run: its row, the pathway version every test or conversation loads, and its work in result order
    (``test_ids`` for a unit run, ``simulations`` for a simulation run, either or both for a regression run;
    ``publish`` only on a blocking regression run)."""

    model_config = ConfigDict(frozen=True)

    run_id: uuid.UUID
    org_id: uuid.UUID
    pathway_id: uuid.UUID
    version_number: int = Field(ge=1)
    kind: RunKind = "unit"
    test_ids: tuple[uuid.UUID, ...] = Field(default=(), max_length=MAX_RUN_TESTS)
    simulations: tuple[SimulationJob, ...] = Field(default=(), max_length=MAX_SIMULATIONS)
    publish: RegressionPublish | None = None

    @model_validator(mode="after")
    def _work_matches_kind(self) -> PathwayTestWorkflowInput:
        if self.kind == "unit" and (not self.test_ids or self.simulations):
            msg = "a unit run needs test_ids and no simulations"
            raise ValueError(msg)
        if self.kind == "simulation" and (not self.simulations or self.test_ids):
            msg = "a simulation run needs simulations and no test_ids"
            raise ValueError(msg)
        if self.kind == "regression" and not (self.test_ids or self.simulations):
            msg = "a regression run needs test_ids or simulations"
            raise ValueError(msg)
        if self.publish is not None and self.kind != "regression":
            msg = "only a regression run completes a publish"
            raise ValueError(msg)
        return self

    def workflow_id(self) -> str:
        return pathway_test_workflow_id(self.run_id)


def run_status(statuses: Iterable[str | None]) -> FinishedRunStatus:
    """The run's status from its results' statuses: ``failed`` when any failed, else ``error`` when any is not
    ``passed`` (an error or a missing status), else ``passed``."""
    seen = list(statuses)
    if "failed" in seen:
        return "failed"
    if any(status != "passed" for status in seen):
        return "error"
    return "passed"


class RegressionSettings(BaseModel):
    """A pathway's regression suite settings (specs/12 §9; Decision P07-T11e1 there): whether a publish runs
    the suite, whether a failing suite blocks it, whether the suite adds simulations, and the scenarios those
    simulations play (one conversation each)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_on_publish: bool = False
    block_publish_on_failure: bool = True
    include_simulations: bool = False
    scenarios: tuple[Scenario, ...] = Field(default=(), max_length=MAX_REGRESSION_SCENARIOS)


def load_regression(global_config: Mapping[str, object]) -> RegressionSettings:
    """The settings stored under ``global_config["regression"]``; a missing or invalid value reads as the
    defaults (imported graphs may carry anything there)."""
    stored = global_config.get(REGRESSION_KEY)
    if not isinstance(stored, Mapping):
        return RegressionSettings()
    try:
        return RegressionSettings.model_validate(stored)
    except ValidationError:
        return RegressionSettings()


def with_regression(global_config: Mapping[str, Any], settings: RegressionSettings) -> dict[str, Any]:
    """A copy of ``global_config`` with ``settings`` stored under the regression key."""
    updated = dict(global_config)
    updated[REGRESSION_KEY] = settings.model_dump(mode="json")
    return updated


def regression_summary(settings: RegressionSettings) -> dict[str, bool | int]:
    """The audit shape of ``settings``: the three flags and the scenario count."""
    return {
        "run_on_publish": settings.run_on_publish,
        "block_publish_on_failure": settings.block_publish_on_failure,
        "include_simulations": settings.include_simulations,
        "scenarios": len(settings.scenarios),
    }


def regression_enabled(run_regression: bool | None, settings: RegressionSettings) -> bool:
    """Whether a publish runs the regression suite: the request asked for it, or the suite opted in with
    ``run_on_publish`` (a request's explicit ``false`` never skips a suite that opted in)."""
    return run_regression is True or settings.run_on_publish


def regression_jobs(settings: RegressionSettings) -> tuple[SimulationJob, ...]:
    """The suite's simulations: one conversation per scenario with ``include_simulations``, else none."""
    if not settings.include_simulations:
        return ()
    return simulation_jobs(settings.scenarios, 1)
