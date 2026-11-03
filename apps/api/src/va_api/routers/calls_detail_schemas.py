"""Query and response models of the call artifact endpoints: active calls, recording, transcript, events, and
the transcript token (specs/05-calls-api.md §7.4, §7.5, §7.6, §7.9; Decision P05-T16b;
specs/19-events-and-webhooks.md §9 Decision P06-T06b; legacy envelope)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from va_domain.call_object import Transcript

__all__ = [
    "DEFAULT_EVENTS_LIMIT",
    "MAX_EVENTS_LIMIT",
    "ActiveCallItem",
    "ActiveCallsResponse",
    "CallEventItem",
    "CallEventsQuery",
    "CallEventsResponse",
    "RecordingQuery",
    "RecordingResponse",
    "TranscriptFormat",
    "TranscriptQuery",
    "TranscriptResponse",
    "TranscriptTokenResponse",
]

DEFAULT_EVENTS_LIMIT: Final = 100
MAX_EVENTS_LIMIT: Final = 1000
_CALL_ID: Final = "0193c5d2-7b1e-7a10-9c4e-3f1d2a6b8c01"
_CREATED_AT: Final = "2026-10-24T18:20:09.000000Z"

type TranscriptFormat = Literal["json", "text", "srt", "vtt"]


def _examples(example: dict[str, JsonValue]) -> ConfigDict:
    return ConfigDict(populate_by_name=True, json_schema_extra={"examples": [example]})


def _query_config() -> ConfigDict:
    return ConfigDict(extra="ignore")


_ACTIVE_ITEM_EXAMPLE: Final[dict[str, JsonValue]] = {
    "call_id": _CALL_ID,
    "to": "+14155550100",
    "from": "+14155550199",
    "status": "in_progress",
    "started_at": _CREATED_AT,
    "direction": "outbound",
    "runner_type": "pathway",
    "pathway_id": "0193c5d2-7b1e-7a10-9c4e-3f1d2a6b8c02",
    "current_node_id": "qualify",
    "duration_seconds": 42,
}


class ActiveCallItem(BaseModel):
    """A call that is dialing, ringing, in progress, or being transferred."""

    model_config = _examples(_ACTIVE_ITEM_EXAMPLE)

    call_id: uuid.UUID = Field(description="Call id.")
    to: str = Field(description="Callee number (E.164; empty for web calls).")
    from_: str = Field(validation_alias="from", serialization_alias="from", description="Caller id (E.164).")
    status: Literal["dialing", "ringing", "in_progress", "transferring"] = Field(
        description="Internal call status."
    )
    started_at: datetime | None = Field(description="Answer time, else dial start (UTC).")
    direction: Literal["outbound", "inbound", "web"] = Field(description="Call direction.")
    runner_type: Literal["task", "pathway", "persona", "agent"] = Field(description="What runs the call.")
    pathway_id: uuid.UUID | None = Field(description="Pathway run by the call.")
    current_node_id: str | None = Field(description="Pathway node the call is on (pathway calls).")
    duration_seconds: int = Field(description="Whole seconds since the answer (0 before it).")


class ActiveCallsResponse(BaseModel):
    """The org's active calls, oldest first."""

    model_config = _examples({"status": "success", "count": 1, "calls": [_ACTIVE_ITEM_EXAMPLE]})

    status: Literal["success"] = "success"
    count: int
    calls: list[ActiveCallItem]


class RecordingQuery(BaseModel):
    """``format=json`` answers the link as JSON instead of redirecting; ``channel`` picks the audio."""

    model_config = _query_config()

    format: Literal["json"] | None = Field(default=None, description="`json` for a JSON body, else 302.")
    channel: Literal["mixed", "caller", "agent"] = Field(default="mixed", description="Audio channel(s).")


class RecordingResponse(BaseModel):
    """A 15-minute presigned link to the recording."""

    model_config = _examples(
        {
            "status": "success",
            "url": "https://recordings.s3.amazonaws.com/org/2026/10/call.wav?X-Amz-Signature=…",
            "expires_at": "2026-10-24T18:35:09.000000Z",
            "duration_ms": 143200,
            "channels": 2,
            "format": "wav",
        }
    )

    status: Literal["success"] = "success"
    url: str = Field(description="Presigned GET URL.")
    expires_at: datetime = Field(description="When the URL stops working (UTC).")
    duration_ms: int | None = Field(description="Recording length, when known.")
    channels: Literal[1, 2] = Field(description="2 for the dual-channel original, 1 for a variant.")
    format: Literal["wav"] = "wav"


class TranscriptQuery(BaseModel):
    """The download format."""

    model_config = _query_config()

    format: TranscriptFormat = Field(default="json", description="`json`, `text`, `srt`, or `vtt`.")


_TRANSCRIPT_EXAMPLE: Final[dict[str, JsonValue]] = {
    "status": "success",
    "call_id": _CALL_ID,
    "transcripts": [
        {
            "id": 1,
            "user": "assistant",
            "text": "Hi, this is Ava from Acme Dental.",
            "created_at": _CREATED_AT,
            "start_ms": 1100,
            "end_ms": 3450,
            "interrupted": False,
        }
    ],
    "concatenated_transcript": "assistant: Hi, this is Ava from Acme Dental.\n",
}


class TranscriptResponse(BaseModel):
    """The call's transcript, as in the Call Object."""

    model_config = _examples(_TRANSCRIPT_EXAMPLE)

    status: Literal["success"] = "success"
    call_id: uuid.UUID
    transcripts: list[Transcript]
    concatenated_transcript: str


class CallEventsQuery(BaseModel):
    """Paging and filters of the event log."""

    model_config = _query_config()

    after_seq: int = Field(default=0, ge=0, description="Return events after this sequence number.")
    limit: int = Field(default=DEFAULT_EVENTS_LIMIT, ge=1, le=MAX_EVENTS_LIMIT, description="Page size.")
    category: str | None = Field(default=None, description="Comma-separated event categories.")


_EVENT_EXAMPLE: Final[dict[str, JsonValue]] = {
    "seq": 3,
    "category": "call",
    "level": "info",
    "message": "answered",
    "data": {"answered_by": "human"},
    "created_at": _CREATED_AT,
}


class CallEventItem(BaseModel):
    """One entry of the call's event log."""

    model_config = _examples(_EVENT_EXAMPLE)

    seq: int = Field(description="Per-call sequence number.")
    category: str = Field(description="Event category.")
    level: Literal["debug", "info", "warning", "error"] = Field(description="Severity.")
    message: str = Field(description="Short event name.")
    data: dict[str, Any] = Field(description="Event details.")
    created_at: datetime = Field(description="When the event happened (UTC).")


class CallEventsResponse(BaseModel):
    """A page of events ordered by ``seq``; ``next_after_seq`` is ``null`` on the last page."""

    model_config = _examples({"status": "success", "events": [_EVENT_EXAMPLE], "next_after_seq": None})

    status: Literal["success"] = "success"
    events: list[CallEventItem]
    next_after_seq: int | None


class TranscriptTokenResponse(BaseModel):
    """A short-lived token for the call's transcript WebSocket."""

    model_config = _examples(
        {
            "status": "success",
            "token": "vtt_example-transcript-token",
            "expires_at": _CREATED_AT,
            "ws_url": f"/v1/calls/{_CALL_ID}/transcript/ws?token=vtt_example-transcript-token",
        }
    )

    status: Literal["success"] = "success"
    token: str | None = Field(
        description="The token (60 s, this call only); `null` on an `Idempotency-Key` replay."
    )
    expires_at: datetime = Field(description="When the token stops opening connections (UTC).")
    ws_url: str | None = Field(
        description="Path of the transcript WebSocket with the token; `null` on an `Idempotency-Key` replay."
    )
