"""Structured outputs, pure parts: registry, tool schemas, JSON extraction, validation and error rendering
(specs/10 §3; Decision 2026-11-01 P03-T14a rules 2-3, 6-7)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import orjson
import pytest
from pydantic import BaseModel, ConfigDict, Field
from structured_models import Citations, GuardRails, Loop, Route

from va_llm import TOOL_NAMES, register_structured_function, registered_schema, tool_schema
from va_llm.structured import (
    MAX_ERROR_CHARS,
    InvalidOutputError,
    check_function,
    first_json_object,
    json_mode_instruction,
    repair_message,
    validate_arguments,
)

SPEC = Path(__file__).resolve().parents[3] / "specs" / "10-llm-gateway.md"


def _walk(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, dict):
        return [node, *(item for value in node.values() for item in _walk(value))]
    if isinstance(node, list):
        return [item for value in node for item in _walk(value)]
    return []


class _Inner(BaseModel):
    code: str


class _Outer(BaseModel):
    first: _Inner = Field(description="the first one")
    many: list[_Inner]
    maybe: _Inner | None = None
    counts: dict[str, int] = Field(default_factory=dict)


class _Node(BaseModel):
    label: str
    children: list[_Node]


class _Pair(BaseModel):
    left: _Node2


class _Node2(BaseModel):
    back: _Pair | None = None


def test_tool_names_equal_the_spec_paragraph() -> None:
    text = SPEC.read_text(encoding="utf-8")
    paragraph = text.split("Registered names:", 1)[1].split(". A test", 1)[0]
    assert frozenset(re.findall(r"`([a-z_]+)`", paragraph)) == TOOL_NAMES
    assert len(TOOL_NAMES) == 14


def test_tool_schema_inlines_every_definition() -> None:
    schema = tool_schema(_Outer)
    nodes = _walk(schema)
    assert not any("$ref" in node or "$defs" in node for node in nodes)
    first = schema["properties"]["first"]
    assert first["description"] == "the first one"
    assert first["properties"]["code"]["type"] == "string"
    assert schema["properties"]["many"]["items"]["additionalProperties"] is False
    maybe = schema["properties"]["maybe"]["anyOf"]
    assert maybe[0]["additionalProperties"] is False
    assert maybe[1] == {"type": "null"}


def test_every_object_with_properties_forbids_extra_keys() -> None:
    schema = tool_schema(_Outer)
    objects = [
        node for node in _walk(schema) if "properties" in node and isinstance(node["properties"], dict)
    ]
    assert len(objects) == 4
    assert all(node["additionalProperties"] is False for node in objects)
    assert schema["properties"]["counts"]["additionalProperties"] == {"type": "integer"}


def test_a_field_named_like_a_keyword_is_not_a_schema() -> None:
    class Odd(BaseModel):
        properties: str
        items: int

    schema = tool_schema(Odd)
    assert schema["properties"]["properties"] == {"title": "Properties", "type": "string"}
    assert "additionalProperties" not in schema["properties"]


def test_explicit_additional_properties_and_boolean_schemas_are_kept() -> None:
    class Open(BaseModel):
        model_config = ConfigDict(extra="allow")
        name: str

    assert tool_schema(Open)["additionalProperties"] is True


def test_references_outside_the_definitions_are_rejected() -> None:
    class External(BaseModel):
        link: str = Field(json_schema_extra={"$ref": "https://schemas.test/link"})

    with pytest.raises(ValueError, match="unsupported \\$ref"):
        tool_schema(External)


def test_recursive_models_are_rejected() -> None:
    with pytest.raises(ValueError, match="recursive schema"):
        tool_schema(_Node)
    with pytest.raises(ValueError, match="recursive schema"):
        tool_schema(_Pair)


def test_tool_schema_returns_a_fresh_copy() -> None:
    first = tool_schema(Route)
    first["properties"].clear()
    assert tool_schema(Route)["properties"]


def test_json_mode_instruction_carries_the_compact_schema() -> None:
    instruction = json_mode_instruction(Loop)
    assert instruction.startswith("Respond with only a JSON object that matches this JSON Schema: {")
    assert instruction.split("JSON Schema: ", 1)[1] == orjson.dumps(tool_schema(Loop)).decode()


def test_repair_message_is_the_spec_text() -> None:
    assert repair_message("score_condition", ["score: 11 is greater than the maximum of 10", "$: bad"]) == (
        "Your previous output was invalid: score: 11 is greater than the maximum of 10; $: bad. "
        "Call score_condition again with corrected arguments."
    )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('Sure! {"a": 1} hope that helps', '{"a": 1}'),
        ('{"text": "a } b {", "n": {"m": [1, 2]}}', '{"text": "a } b {", "n": {"m": [1, 2]}}'),
        ('Use {braces} then {"ok": true}', '{"ok": true}'),
        ('{"a": 1} and {"b": 2}', '{"a": 1}'),
        ("no json here", None),
        ("{not: json}", None),
        ("", None),
        (None, None),
    ],
)
def test_first_json_object(content: str | None, expected: str | None) -> None:
    assert first_json_object(content) == expected


def test_valid_arguments_become_the_model() -> None:
    route = validate_arguments(
        Route,
        '{"path_id":"stay","confidence":7,"alternatives":[{"path_id":"x","confidence":1}],"reason":"r"}',
    )
    assert (route.path_id, route.alternatives[0].confidence) == ("stay", 1)


@pytest.mark.parametrize(
    ("text", "errors"),
    [
        ("[1, 2]", ("$: arguments are not a JSON object",)),
        (
            '{"score": 11, "met": true, "missing": ""}',
            ("score: 11 is greater than the maximum of 10",),
        ),
        (
            '{"score": 1, "met": true}',
            ("$: 'missing' is a required property",),
        ),
        (
            '{"score": 1, "met": true, "missing": "", "extra": 1}',
            ("$: Additional properties are not allowed ('extra' was unexpected)",),
        ),
        (
            '{"score": 1, "met": true, "missing": "LOUD"}',
            ("missing: Value error, must not be upper case",),
        ),
    ],
)
def test_invalid_arguments_render_their_errors(text: str, errors: tuple[str, ...]) -> None:
    with pytest.raises(InvalidOutputError) as caught:
        validate_arguments(Loop, text)
    assert caught.value.errors == errors


def test_non_json_arguments() -> None:
    with pytest.raises(InvalidOutputError) as caught:
        validate_arguments(Loop, "not json")
    [error] = caught.value.errors
    assert error.startswith("$: arguments are not valid JSON (")


def test_nested_paths_and_the_error_cap() -> None:
    results = [{"name": "x", "triggered": "no", "confidence": 2, "evidence": "e"} for _ in range(3)]
    with pytest.raises(InvalidOutputError) as caught:
        validate_arguments(GuardRails, '{"results": ' + json.dumps(results) + "}")
    errors = caught.value.errors
    assert len(errors) == 5
    assert errors[0] == "results[0].confidence: 2 is greater than the maximum of 1"
    assert errors[1] == "results[0].triggered: 'no' is not of type 'boolean'"
    assert errors == tuple(sorted(errors))


def test_long_messages_are_clipped() -> None:
    long_name = "v" * 400
    with pytest.raises(InvalidOutputError) as caught:
        validate_arguments(
            Citations, '{"citations": [{"variable_name": "' + long_name + '", "turn_numbers": []}]}'
        )
    [error] = caught.value.errors
    message = error.split(": ", 1)[1]
    assert len(message) == MAX_ERROR_CHARS
    assert message.endswith("…")


def test_registry_rules() -> None:
    assert registered_schema("select_path") is None
    register_structured_function("select_path", Route)
    register_structured_function("select_path", Route)
    assert registered_schema("select_path") is Route
    with pytest.raises(ValueError, match="registered with Route, not Loop"):
        register_structured_function("select_path", Loop)
    with pytest.raises(ValueError, match="not a registered structured function name"):
        register_structured_function("pick_path", Route)


def test_check_function() -> None:
    check_function("score_condition", Loop)
    register_structured_function("score_condition", Loop)
    check_function("score_condition", Loop)
    with pytest.raises(ValueError, match="registered with Loop, not Route"):
        check_function("score_condition", Route)
    with pytest.raises(ValueError, match="not a registered structured function name"):
        check_function("nope", Loop)
