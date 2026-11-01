"""Bodies of the Bland-compatible ``POST /v1/calls`` (specs/05-calls-api.md §5; legacy envelope)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

__all__ = ["SendCallResponse"]

_QUEUED_EXAMPLE: Final[dict[str, JsonValue]] = {
    "status": "success",
    "message": "Call successfully queued.",
    "call_id": "0193c5d2-7b1e-7a10-9a52-1f3b0d4e6a77",
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
