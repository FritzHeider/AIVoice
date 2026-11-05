"""Inline tools and dynamic data specs carried by Send Call (specs/13-tools-and-dynamic-data.md §2, §7).

These are the request/config shapes only; execution and JSONPath parsing live in `va_tools`, which
re-exports them instead of redefining them.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, StringConstraints

__all__ = [
    "HEADER_NAME",
    "KB_REF_PATTERN",
    "TOOL_REF_PATTERN",
    "DynamicDataSpec",
    "HttpMethod",
    "InlineTool",
    "KbRef",
    "ResponseDataItem",
    "ToolRef",
    "VariableName",
]

_UUID: Final = r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
TOOL_REF_PATTERN: Final = rf"^(TL|KB)-{_UUID}$"
KB_REF_PATTERN: Final = rf"^KB-{_UUID}$"
_MAX_BODY_BYTES: Final = 64 * 1024
# RFC 7230 token characters.
HEADER_NAME: Final = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")

ToolRef = Annotated[str, StringConstraints(pattern=TOOL_REF_PATTERN)]
KbRef = Annotated[str, StringConstraints(pattern=KB_REF_PATTERN)]
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
VariableName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]


def _templated_http_url(value: str) -> str:
    # The URL may hold `{{vars}}` until render time, so only the scheme prefix is checked here.
    if not value.lower().startswith(("http://", "https://")):
        msg = "must start with http:// or https://"
        raise ValueError(msg)
    return value


def _header_names(headers: dict[str, str]) -> dict[str, str]:
    for name in headers:
        if not HEADER_NAME.match(name):
            msg = f"header name {name!r} is not an RFC 7230 token"
            raise ValueError(msg)
    return headers


def _body_size(body: JsonValue) -> JsonValue:
    size = len(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode())
    if size > _MAX_BODY_BYTES:
        msg = f"body is {size} bytes serialized; the limit is {_MAX_BODY_BYTES}"
        raise ValueError(msg)
    return body


def _object_schema(schema: dict[str, JsonValue]) -> dict[str, JsonValue]:
    if schema.get("type") != "object":
        msg = 'input_schema must be a JSON Schema with "type": "object"'
        raise ValueError(msg)
    return schema


TemplatedUrl = Annotated[
    str, StringConstraints(min_length=1, max_length=2048), AfterValidator(_templated_http_url)
]
Headers = Annotated[dict[str, str], AfterValidator(_header_names)]
Body = Annotated[JsonValue, AfterValidator(_body_size)]


def _empty_object_schema() -> dict[str, JsonValue]:
    return {"type": "object", "properties": {}}


class InlineTool(BaseModel):
    """A tool defined inside the request (ToolCreate without `id` and without `secret_headers`, which would
    be stored in plaintext in `calls.config`)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$", description="Function name shown to the LLM.")
    description: str = Field(min_length=1, max_length=1000, description="When the LLM should call the tool.")
    speech: str | None = Field(default=None, max_length=500, description="Said while the tool runs.")
    url: TemplatedUrl = Field(description="Request URL; `{{vars}}` are rendered at call time.")
    method: HttpMethod = "POST"
    headers: Headers = Field(default_factory=dict)
    query: dict[str, str] = Field(default_factory=dict)
    body: Body = None
    input_schema: Annotated[dict[str, JsonValue], AfterValidator(_object_schema)] = Field(
        default_factory=_empty_object_schema, description="JSON Schema (draft 2020-12) of the tool input."
    )
    response: dict[str, str] = Field(default_factory=dict, description="Variable name -> JSONPath.")
    timeout_ms: int = Field(default=10_000, ge=100, le=60_000)
    retries: int = Field(default=0, ge=0, le=3)


class ResponseDataItem(BaseModel):
    """One variable extracted from a dynamic data response."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: VariableName
    data: str = Field(min_length=1, max_length=500, description="JSONPath into the response.")
    context: str | None = Field(default=None, max_length=1000, description="Appended to the prompt context.")


class DynamicDataSpec(BaseModel):
    """A pre-call (or per-turn) HTTP fetch whose extracted values become variables (specs/13 §7)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: TemplatedUrl
    method: HttpMethod = "GET"
    headers: Headers = Field(default_factory=dict)
    query: dict[str, str] = Field(default_factory=dict)
    body: Body = None
    cache: bool = False
    cache_ttl_seconds: int = Field(default=300, ge=0, le=86_400)
    timing: Literal["pre_call", "every_turn"] = "pre_call"
    response_data: list[ResponseDataItem] = Field(default_factory=list, max_length=20)
