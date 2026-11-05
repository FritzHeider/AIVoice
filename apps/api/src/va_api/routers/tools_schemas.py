"""Request and response bodies of the tools API (specs/13-tools-and-dynamic-data.md §2; legacy bodies).

``ToolCreate`` is the Send Call inline tool (:class:`va_domain.calls.tools.InlineTool`, which validates every
field shape) plus write-only ``secret_headers``. ``ToolUpdate`` takes any subset of the same fields: the
service merges it over the stored definition and validates the result as a whole, so field rules are not
repeated here. Responses show secret header values masked (specs/13 §2 Decision P08-T01). The ``/test`` bodies
follow specs/13 §5 Decision P08-T02a.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from va_domain.calls.tools import HttpMethod, InlineTool

__all__ = [
    "ToolCreate",
    "ToolCreated",
    "ToolDeleted",
    "ToolEnvelope",
    "ToolList",
    "ToolTestRequest",
    "ToolTestRequestView",
    "ToolTestResult",
    "ToolUpdate",
    "ToolView",
]

_TOOL_ID: Final = "TL-0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d71"
_DEFINITION: Final[dict[str, Any]] = {
    "name": "check_availability",
    "description": "Checks table availability for a date, time and party size.",
    "speech": "Let me check that for you.",
    "url": "https://api.bellapasta.com/availability",
    "method": "POST",
    "headers": {"Content-Type": "application/json"},
    "query": {},
    "body": {
        "date": "{{input.date}}",
        "time": "{{input.time}}",
        "party": "{{input.party_size}}",
        "caller": "{{from}}",
    },
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "time": {"type": "string", "description": "HH:MM 24h"},
            "party_size": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["date", "time", "party_size"],
    },
    "response": {"available": "$.available", "alternatives": "$.alternatives[*].time"},
    "timeout_ms": 8000,
    "retries": 1,
}
_VIEW_EXAMPLE: Final[dict[str, Any]] = {
    "tool_id": _TOOL_ID,
    **_DEFINITION,
    "secret_headers": {"Authorization": "••••alue"},
    "created_at": "2026-11-05T09:15:00Z",
    "updated_at": "2026-11-05T09:15:00Z",
}
_WRITE_ONLY_HEADERS: Final = (
    "Write-only headers stored encrypted; responses show `••••` plus the last 4 characters "
    "(values of at least 12 characters) or `••••`."
)


class ToolCreate(InlineTool):
    """``POST /v1/tools``: a tool definition (specs/13 §2)."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [{**_DEFINITION, "secret_headers": {"Authorization": "Bearer example-secret-value"}}]
        },
    )

    secret_headers: dict[str, str] = Field(default_factory=dict, repr=False, description=_WRITE_ONLY_HEADERS)


class ToolUpdate(BaseModel):
    """``POST /v1/tools/{tool_id}``: any subset of the ToolCreate fields.

    Omitted fields keep their stored value. ``null`` clears ``speech``, ``body``, and ``secret_headers``; the
    other fields reject it. ``secret_headers`` replaces the whole stored set.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"description": "Checks table availability.", "timeout_ms": 5000}]},
    )

    name: str | None = None
    description: str | None = None
    speech: str | None = None
    url: str | None = None
    method: HttpMethod | None = None
    headers: dict[str, str] | None = None
    secret_headers: dict[str, str] | None = Field(default=None, repr=False, description=_WRITE_ONLY_HEADERS)
    query: dict[str, str] | None = None
    body: JsonValue = None
    input_schema: dict[str, JsonValue] | None = None
    response: dict[str, str] | None = None
    timeout_ms: int | None = None
    retries: int | None = None


class ToolView(BaseModel):
    """A stored tool; ``secret_headers`` values are always masked."""

    model_config = ConfigDict(json_schema_extra={"examples": [_VIEW_EXAMPLE]})

    tool_id: str = Field(description="`TL-<uuid>`.")
    name: str
    description: str
    speech: str | None
    url: str
    method: HttpMethod
    headers: dict[str, str]
    secret_headers: dict[str, str] = Field(description=_WRITE_ONLY_HEADERS)
    query: dict[str, str]
    body: JsonValue
    input_schema: dict[str, JsonValue]
    response: dict[str, str] = Field(description="Variable name -> JSONPath (the `response_map` column).")
    timeout_ms: int
    retries: int
    created_at: datetime
    updated_at: datetime


class ToolCreated(BaseModel):
    """``POST /v1/tools`` result."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"status": "success", "tool_id": _TOOL_ID, "tool": _VIEW_EXAMPLE}]}
    )

    status: Literal["success"] = "success"
    tool_id: str
    tool: ToolView


class ToolEnvelope(BaseModel):
    """``GET``/``POST /v1/tools/{tool_id}`` result."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"status": "success", "tool": _VIEW_EXAMPLE}]})

    status: Literal["success"] = "success"
    tool: ToolView


class ToolList(BaseModel):
    """``GET /v1/tools`` result: newest first; ``next_cursor`` is null on the last page."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"status": "success", "tools": [_VIEW_EXAMPLE], "next_cursor": None}]}
    )

    status: Literal["success"] = "success"
    tools: list[ToolView]
    next_cursor: str | None


class ToolDeleted(BaseModel):
    """``DELETE /v1/tools/{tool_id}`` result."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"status": "success", "message": "Tool deleted."}]}
    )

    status: Literal["success"] = "success"
    message: str


_TEST_REQUEST_EXAMPLE: Final[dict[str, Any]] = {
    "url": "https://api.bellapasta.com/availability",
    "method": "POST",
    "headers": {"Content-Type": "application/json", "Authorization": "••••"},
    "body": {"date": "2026-11-06", "time": "19:30", "party": 4, "caller": ""},
}


class ToolTestRequest(BaseModel):
    """``POST /v1/tools/{tool_id}/test``: the tool input (the model's arguments) and template variables."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "input": {"date": "2026-11-06", "time": "19:30", "party_size": 4},
                    "variables": {"customer_name": "Ada"},
                }
            ]
        },
    )

    input: dict[str, JsonValue] = Field(
        default_factory=dict, description="Arguments validated against `input_schema` (`{{input.*}}`)."
    )
    variables: dict[str, JsonValue] = Field(
        default_factory=dict, description="Conversation variables the templates may read."
    )


class ToolTestRequestView(BaseModel):
    """The request as sent: full URL, masked secret header values."""

    model_config = ConfigDict(json_schema_extra={"examples": [_TEST_REQUEST_EXAMPLE]})

    url: str
    method: HttpMethod
    headers: dict[str, str]
    body: JsonValue


class ToolTestResult(BaseModel):
    """``POST /v1/tools/{tool_id}/test`` result.

    ``error`` is null for a 2xx answer, else ``invalid_arguments`` (``details`` lists why; nothing is sent),
    ``http_error``, ``timeout``, ``connection``, ``blocked``, ``invalid_url``, or ``render_error``.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "success",
                    "request": _TEST_REQUEST_EXAMPLE,
                    "status_code": 200,
                    "response": {"available": False, "alternatives": [{"time": "20:30"}]},
                    "extracted": {"available": False, "alternatives": "20:30"},
                    "duration_ms": 182,
                    "error": None,
                    "details": [],
                    "truncated": False,
                }
            ]
        }
    )

    status: Literal["success"] = "success"
    request: ToolTestRequestView | None
    status_code: int | None
    response: JsonValue
    extracted: dict[str, JsonValue] = Field(description="Every `response` variable (null = no match).")
    duration_ms: int
    error: str | None
    details: list[dict[str, JsonValue]] = Field(description="Input validation failures (`path`, `message`).")
    truncated: bool = Field(description="Whether the response exceeded 1 MiB and was cut.")
