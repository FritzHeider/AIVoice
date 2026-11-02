"""``CallWorkflow`` start contract (specs/05 §5)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from va_domain.call_workflow import (
    CALL_WORKFLOW_NAME,
    CANCEL_SIGNAL,
    PIPELINE_FINISHED_SIGNAL,
    CallWorkflowInput,
    PipelineFinished,
    call_workflow_id,
    max_duration_seconds,
)
from va_domain.post_call import (
    POSTCALL_WORKFLOW_NAME,
    PostCallWorkflowInput,
    postcall_task_queue,
    postcall_workflow_id,
)

CALL = uuid.UUID("01890a5d-ac96-774b-bcce-b302099a8057")
ORG = uuid.UUID("01890a5d-ac96-774b-bcce-b302099a8058")


def test_the_input_round_trips_through_json() -> None:
    scheduled = datetime(2026, 11, 2, 15, 0, tzinfo=timezone(timedelta(hours=-5)))
    original = CallWorkflowInput(call_id=CALL, org_id=ORG, scheduled_for=scheduled)
    dumped = original.model_dump(mode="json")
    assert dumped == {
        "call_id": str(CALL),
        "org_id": str(ORG),
        "scheduled_for": "2026-11-02T15:00:00-05:00",
        "direction": "outbound",
        "max_duration_s": None,
    }
    restored = CallWorkflowInput.model_validate(dumped)
    assert restored == original
    assert restored.scheduled_for == scheduled.astimezone(UTC)


def test_scheduled_for_defaults_to_none() -> None:
    assert CallWorkflowInput(call_id=CALL, org_id=ORG).model_dump(mode="json")["scheduled_for"] is None


def test_unknown_fields_and_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        CallWorkflowInput.model_validate({"call_id": str(CALL), "org_id": str(ORG), "extra": 1})
    with pytest.raises(ValidationError):
        CallWorkflowInput(call_id=CALL, org_id=ORG, scheduled_for=datetime(2026, 11, 2))  # noqa: DTZ001


def test_workflow_name_and_id() -> None:
    assert CALL_WORKFLOW_NAME == "CallWorkflow"
    assert call_workflow_id(CALL) == f"call-{CALL}"
    assert CallWorkflowInput(call_id=CALL, org_id=ORG).workflow_id() == f"call-{CALL}"


def test_signal_names() -> None:
    assert CANCEL_SIGNAL == "signal_cancel"
    assert PIPELINE_FINISHED_SIGNAL == "signal_pipeline_finished"


def test_pipeline_finished_round_trips_and_rejects_unknown_fields() -> None:
    finished = PipelineFinished(end_reason="caller_hangup", ended_by="user")
    dumped = finished.model_dump(mode="json")
    assert dumped == {"end_reason": "caller_hangup", "ended_by": "user"}
    assert PipelineFinished.model_validate(dumped) == finished
    with pytest.raises(ValidationError):
        PipelineFinished.model_validate({**dumped, "extra": 1})


def test_post_call_contract() -> None:
    assert POSTCALL_WORKFLOW_NAME == "PostCallWorkflow"
    assert postcall_workflow_id(CALL) == f"postcall-{CALL}"
    restored = PostCallWorkflowInput.model_validate({"call_id": str(CALL), "org_id": str(ORG)})
    assert restored == PostCallWorkflowInput(call_id=CALL, org_id=ORG)


@pytest.mark.parametrize(
    ("main", "postcall"), [("va-main", "va-postcall"), ("staging-va-main", "staging-va-postcall")]
)
def test_postcall_task_queue_replaces_the_main_suffix(main: str, postcall: str) -> None:
    assert postcall_task_queue(main) == postcall


@pytest.mark.parametrize("main", ["-main", "va-postcall", "va-main-x", ""])
def test_postcall_task_queue_needs_a_main_suffix(main: str) -> None:
    with pytest.raises(ValueError, match="must end with '-main'"):
        postcall_task_queue(main)


def test_inbound_inputs_carry_the_max_duration() -> None:
    start = CallWorkflowInput(call_id=CALL, org_id=ORG, direction="inbound", max_duration_s=1800)
    assert CallWorkflowInput.model_validate(start.model_dump(mode="json")) == start


@pytest.mark.parametrize(
    "fields",
    [
        {"direction": "inbound"},
        {"direction": "inbound", "max_duration_s": 60, "scheduled_for": datetime(2026, 11, 2, tzinfo=UTC)},
        {"direction": "outbound", "max_duration_s": 60},
        {"direction": "web"},
    ],
)
def test_direction_rules_are_enforced(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CallWorkflowInput.model_validate({"call_id": str(CALL), "org_id": str(ORG), **fields})


def test_max_duration_seconds_rounds_up() -> None:
    assert (max_duration_seconds(5.0), max_duration_seconds(0.5), max_duration_seconds(1.001)) == (
        300,
        30,
        61,
    )
