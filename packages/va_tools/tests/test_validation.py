"""Tool definition checks beyond field shapes (specs/13 §2, §4; Decision P08-T01)."""

from __future__ import annotations

from typing import Any

import pytest

from va_common.errors import AppError
from va_domain.calls.tools import InlineTool
from va_engine.system_tools import RESERVED_TOOL_NAMES
from va_tools.validation import MAX_SECRET_HEADER_LENGTH, check_secret_headers, check_tool_definition

SPEC_EXAMPLE: dict[str, Any] = {
    "name": "check_availability",
    "description": "Checks table availability for a date, time and party size.",
    "speech": "Let me check that for you.",
    "url": "https://api.bellapasta.com/availability",
    "method": "POST",
    "headers": {"Content-Type": "application/json"},
    "body": {"date": "{{input.date}}", "party": "{{input.party_size}}"},
    "input_schema": {
        "type": "object",
        "properties": {"party_size": {"type": "integer", "minimum": 1, "maximum": 20}},
        "required": ["party_size"],
    },
    "response": {"available": "$.available", "alternatives": "$.alternatives[*].time"},
    "timeout_ms": 8000,
    "retries": 1,
}


def _tool(**changes: Any) -> InlineTool:
    return InlineTool.model_validate({**SPEC_EXAMPLE, **changes})


def _error(
    tool: InlineTool, *, allow_http: bool = False, secret_headers: dict[str, str] | None = None
) -> AppError:
    with pytest.raises(AppError) as caught:
        check_tool_definition(tool, secret_headers=secret_headers, allow_http=allow_http)
    assert caught.value.code == "INVALID_INPUT"
    return caught.value


def test_the_spec_example_passes() -> None:
    check_tool_definition(_tool(), secret_headers={"Authorization": "Bearer x"}, allow_http=False)


@pytest.mark.parametrize("name", sorted(RESERVED_TOOL_NAMES))
def test_reserved_system_tool_names_are_refused(name: str) -> None:
    error = _error(_tool(name=name))
    assert error.field == "name"
    assert "reserved" in error.message


def test_reserved_names_are_case_sensitive() -> None:
    check_tool_definition(_tool(name="End_call"), allow_http=False)


def test_http_is_refused_unless_allowed() -> None:
    tool = _tool(url="http://api.bellapasta.com/availability")
    assert _error(tool).field == "url"
    check_tool_definition(tool, allow_http=True)


def test_templated_hosts_pass() -> None:
    check_tool_definition(_tool(url="https://{{host}}/availability"), allow_http=False)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"a": {"type": "nope"}}},
        {"type": "object", "required": "party_size"},
        {"type": "object", "properties": {"a": {"minimum": "one"}}},
    ],
)
def test_invalid_json_schemas_are_refused(schema: dict[str, Any]) -> None:
    error = _error(_tool(input_schema=schema))
    assert error.field == "input_schema"
    assert error.message.startswith("input_schema is not a valid JSON Schema (draft 2020-12)")


def test_bad_jsonpath_names_the_variable() -> None:
    error = _error(_tool(response={"available": "$.[[["}))
    assert error.field == "response.available"


@pytest.mark.parametrize("key", ["1st", "has-dash", "a" * 65, ""])
def test_response_keys_must_be_variable_names(key: str) -> None:
    assert _error(_tool(response={key: "$.a"})).field == "response"


@pytest.mark.parametrize(
    ("secret_headers", "field", "fragment"),
    [
        ({"Bad Name": "v"}, "secret_headers", "RFC 7230"),
        ({"content-type": "v"}, "secret_headers.content-type", "both headers"),
        ({"Authorization": ""}, "secret_headers.Authorization", "empty"),
        ({"Authorization": "a\r\nX-Injected: 1"}, "secret_headers.Authorization", "line break"),
        ({"Authorization": "a" * (MAX_SECRET_HEADER_LENGTH + 1)}, "secret_headers.Authorization", "longer"),
    ],
)
def test_secret_header_rules(secret_headers: dict[str, str], field: str, fragment: str) -> None:
    error = _error(_tool(), secret_headers=secret_headers)
    assert error.field == field
    assert fragment in error.message


def test_a_secret_header_at_the_length_limit_passes() -> None:
    check_secret_headers({"Authorization": "a" * MAX_SECRET_HEADER_LENGTH}, plain_headers={})
