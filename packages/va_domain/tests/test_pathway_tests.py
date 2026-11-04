"""``PathwayTestWorkflow`` start contract and the run status rule (specs/12 §8; Decision P07-T11c)."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from va_domain.pathway_tests import (
    MAX_RUN_TESTS,
    PathwayTestWorkflowInput,
    pathway_test_workflow_id,
    run_status,
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
