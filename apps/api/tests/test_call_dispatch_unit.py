"""Pure helpers of the Send Call dispatch (specs/05 §5 Decision P05-T05b)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from va_api.routers.calls_schemas import SendCallResponse
from va_api.services import call_dispatch
from va_domain.call_resolution import ResolutionWarning


def test_warnings_carry_the_field_only_when_set() -> None:
    warnings = [ResolutionWarning("A", "first", "task"), ResolutionWarning("B", "second")]
    assert call_dispatch._warnings(warnings) == [
        {"code": "A", "message": "first", "field": "task"},
        {"code": "B", "message": "second"},
    ]


def test_the_response_omits_scheduled_for_unless_scheduled() -> None:
    call_id = uuid.UUID(int=7)
    queued = SendCallResponse(message="Call successfully queued.", call_id=call_id)
    assert queued.model_dump(mode="json") == {
        "status": "success",
        "message": "Call successfully queued.",
        "call_id": str(call_id),
        "batch_id": None,
    }
    scheduled = SendCallResponse(
        message="m", call_id=call_id, scheduled_for=datetime(2026, 11, 2, 16, tzinfo=UTC)
    )
    assert scheduled.model_dump(mode="json")["scheduled_for"] == "2026-11-02T16:00:00Z"
