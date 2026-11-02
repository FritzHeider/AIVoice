"""Bodies of the Bland-compatible ``POST /v1/calls`` and the stop endpoints (specs/05-calls-api.md §5, §7.2,
§7.3; legacy envelope)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

MAX_STOP_CALL_IDS: Final = 1000
_EXAMPLE_CALL_ID: Final = "0193c5d2-7b1e-7a10-9a52-1f3b0d4e6a77"

__all__ = [
    "MAX_STOP_CALL_IDS",
    "SendCallResponse",
    "StopActiveCallsRequest",
    "StopActiveCallsResponse",
    "StopCallResponse",
]

_QUEUED_EXAMPLE: Final[dict[str, JsonValue]] = {
    "status": "success",
    "message": "Call successfully queued.",
    "call_id": _EXAMPLE_CALL_ID,
    "batch_id": None,
}
_SCHEDULED_EXAMPLE: Final[dict[str, JsonValue]] = {
    **_QUEUED_EXAMPLE,
    "message": "Call successfully scheduled.",
    "scheduled_for": "2026-10-25T16:00:00Z",
}


def _is_none(value: object) -> bool:
    return value is None


class SendCallResponse(BaseModel):
    """The created call; ``scheduled_for`` (UTC) appears only for scheduled calls."""

    model_config = ConfigDict(json_schema_extra={"examples": [_QUEUED_EXAMPLE, _SCHEDULED_EXAMPLE]})

    status: Literal["success"] = "success"
    message: str
    call_id: uuid.UUID
    batch_id: uuid.UUID | None = None
    scheduled_for: datetime | None = Field(default=None, exclude_if=_is_none)


class StopCallResponse(BaseModel):
    """The call was canceled (not answered yet) or told to hang up (in progress)."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"status": "success", "message": "Call ended successfully."},
                {"status": "success", "message": "Call canceled."},
            ]
        }
    )

    status: Literal["success"] = "success"
    message: str


class StopActiveCallsRequest(BaseModel):
    """Restrict stop-all to ``call_ids`` (other orgs' and unknown ids are ignored); no body stops all calls.

    At most 1 000 ids; duplicates count once."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"call_ids": [_EXAMPLE_CALL_ID]}]}
    )

    call_ids: list[uuid.UUID] | None = Field(default=None, max_length=MAX_STOP_CALL_IDS)


class StopActiveCallsResponse(BaseModel):
    """The calls that were stopped (oldest first)."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "success",
                    "message": "Stopped 1 calls.",
                    "stopped": 1,
                    "call_ids": [_EXAMPLE_CALL_ID],
                }
            ]
        }
    )

    status: Literal["success"] = "success"
    message: str
    stopped: int
    call_ids: list[uuid.UUID]
