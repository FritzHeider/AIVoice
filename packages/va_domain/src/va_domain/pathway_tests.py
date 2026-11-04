"""The ``PathwayTestWorkflow`` start contract shared by the api (starter) and the worker
(specs/12-pathways-api.md §8; Decision P07-T11c there).

The api inserts a ``pathway_test_runs`` row and starts the workflow with id ``pathway-test-run-<run_id>`` on
the post-call task queue (``va_domain.post_call.postcall_task_queue``). Workflow code imports this module, so
it imports only pydantic.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MAX_RUN_TESTS",
    "PATHWAY_TEST_WORKFLOW_NAME",
    "RUN_CONCURRENCY",
    "FinishedRunStatus",
    "PathwayTestWorkflowInput",
    "pathway_test_workflow_id",
    "run_status",
]

PATHWAY_TEST_WORKFLOW_NAME: Final = "PathwayTestWorkflow"
RUN_CONCURRENCY: Final = 8
"""Tests of one run in flight at once (specs/12 §8)."""
MAX_RUN_TESTS: Final = 500
"""Tests one run may hold."""

type FinishedRunStatus = Literal["passed", "failed", "error"]


def pathway_test_workflow_id(run_id: uuid.UUID) -> str:
    """``pathway-test-run-<run_id>``."""
    return f"pathway-test-run-{run_id}"


class PathwayTestWorkflowInput(BaseModel):
    """One unit run: its row, the pathway version every test loads, and the tests in result order."""

    model_config = ConfigDict(frozen=True)

    run_id: uuid.UUID
    org_id: uuid.UUID
    pathway_id: uuid.UUID
    version_number: int = Field(ge=1)
    test_ids: tuple[uuid.UUID, ...] = Field(min_length=1, max_length=MAX_RUN_TESTS)

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
