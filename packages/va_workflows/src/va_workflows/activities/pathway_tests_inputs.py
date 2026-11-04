"""Inputs, results, timeouts, and retry policies of the ``PathwayTestWorkflow`` activities
(specs/12-pathways-api.md §8 and §9, Decisions P07-T11c and P07-T11d).

Workflows import this module to schedule the activities, so it imports only pydantic, the Temporal
``RetryPolicy``, and the import-free domain models.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict
from temporalio.common import RetryPolicy

from va_domain.pathway_tests import FinishedRunStatus, SimulationJob

__all__ = [
    "BOOKKEEPING_RETRY",
    "BOOKKEEPING_TIMEOUT",
    "SIMULATION_TIMEOUT",
    "TEST_RETRY",
    "TEST_TIMEOUT",
    "FinishInput",
    "FinishResult",
    "RunRef",
    "SimulationErrorInput",
    "SimulationInput",
    "UnitTestErrorInput",
    "UnitTestInput",
    "UnitTestOutcome",
]

TEST_TIMEOUT: Final = timedelta(minutes=30)
"""``run_pathway_unit_test``: 21 cases of at most 60 s each, the variations call, and grading."""
SIMULATION_TIMEOUT: Final = timedelta(minutes=60)
"""``run_pathway_simulation``: up to 50 caller turns of at most 60 s of engine time each, plus the caller
model and grading."""
TEST_RETRY: Final = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=3,
)
BOOKKEEPING_TIMEOUT: Final = timedelta(seconds=30)
"""``begin_pathway_test_run``, ``record_pathway_test_error``, ``record_pathway_simulation_error``,
``finish_pathway_test_run``."""
BOOKKEEPING_RETRY: Final = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=5,
)

type UnitTestOutcome = Literal["passed", "failed", "error", "skipped"]
"""A test's or simulation's result status; ``skipped`` when the run row is gone or finished (nothing was
stored)."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunRef(_Model):
    """A run row."""

    run_id: uuid.UUID
    org_id: uuid.UUID


class UnitTestInput(RunRef):
    """One test of a run against the run's pathway version."""

    pathway_id: uuid.UUID
    version_number: int
    test_id: uuid.UUID


class UnitTestErrorInput(RunRef):
    """A test whose activity failed after its retries: stored as an ``error`` item with ``error``."""

    test_id: uuid.UUID
    error: Literal["runner_error"] = "runner_error"


class SimulationInput(RunRef):
    """One conversation of a simulation run against the run's pathway version."""

    pathway_id: uuid.UUID
    version_number: int
    job: SimulationJob


class SimulationErrorInput(RunRef):
    """A conversation whose activity failed after its retries: stored as an ``error`` item with ``error``."""

    job: SimulationJob
    error: Literal["runner_error"] = "runner_error"


class FinishInput(RunRef):
    """The run's work in result order: test ids, then simulation ids."""

    test_ids: tuple[uuid.UUID, ...] = ()
    simulation_ids: tuple[str, ...] = ()


class FinishResult(_Model):
    """The final status; ``None`` when the run row is gone or was already finished."""

    status: FinishedRunStatus | None
