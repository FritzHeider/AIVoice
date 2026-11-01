"""``CallWorkflow`` start contract (specs/05 §5)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from va_domain.call_workflow import CALL_WORKFLOW_NAME, CallWorkflowInput, call_workflow_id

CALL = uuid.UUID("01890a5d-ac96-774b-bcce-b302099a8057")
ORG = uuid.UUID("01890a5d-ac96-774b-bcce-b302099a8058")


def test_the_input_round_trips_through_json() -> None:
    scheduled = datetime(2026, 11, 2, 15, 0, tzinfo=timezone(timedelta(hours=-5)))
    original = CallWorkflowInput(call_id=CALL, org_id=ORG, scheduled_for=scheduled)
    dumped = original.model_dump(mode="json")
    assert dumped == {"call_id": str(CALL), "org_id": str(ORG), "scheduled_for": "2026-11-02T15:00:00-05:00"}
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
