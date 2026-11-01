"""Schema-valid tool arguments (specs/30 §3.4 Decision P03-T06b rule 3), cross-checked with ``jsonschema``."""

from __future__ import annotations

from typing import Any

import jsonschema
import pytest
from llm_schemas import SPEC_CALLS

from va_sim.llm.schema_fill import accepts, conform, default_for, property_schemas, value_type

EDGE_SCHEMAS: list[dict[str, Any]] = [
    {"type": "string", "minLength": 3},
    {"type": ["string", "null"], "maxLength": 2},
    {"type": "integer", "exclusiveMinimum": 2.5, "maximum": 9},
    {"type": "integer", "minimum": 0.5, "exclusiveMaximum": 3},
    {"type": "number", "exclusiveMinimum": 1, "exclusiveMaximum": 2},
    {"type": "number", "exclusiveMaximum": -1},
    {"type": "number", "minimum": -5, "exclusiveMaximum": -1},
    {"type": "integer", "maximum": -4},
    {"enum": ["a", "b"]},
    {"type": ["string", "null"], "enum": ["a", None]},
    {"const": 3},
    {"default": "d", "type": "string"},
    {"type": "string", "pattern": "^[0-9]+$", "examples": ["42"]},
    {"type": "array", "minItems": 2, "items": {"type": "boolean"}},
    {
        "type": "array",
        "prefixItems": [{"type": "integer"}, {"type": "string"}],
        "minItems": 2,
        "items": False,
    },
    {"anyOf": [{"type": "integer", "minimum": 5}, {"type": "string"}]},
    {"anyOf": [{"type": "string"}, {"type": "null"}]},
    {"oneOf": [{"type": "boolean"}, {"type": "string", "minLength": 1}]},
    {"allOf": [{"type": "integer"}, {"minimum": 2}]},
    {
        "type": "object",
        "properties": {"a": {"$ref": "#/$defs/A"}},
        "required": ["a"],
        "$defs": {"A": {"type": "integer"}},
    },
    {
        "type": "object",
        "properties": {"a": {"$ref": "#/definitions/B"}},
        "required": ["a"],
        "definitions": {"B": True},
    },
    {"type": "object", "required": ["x"], "additionalProperties": {"type": "integer"}},
    {"properties": {"z": {"type": "boolean"}}, "required": ["z"]},
    {"type": "object", "properties": {"r": {"$ref": "#"}}},
    {"type": "array", "items": {"type": "integer"}},
]
CANDIDATES: list[Any] = [
    None,
    "abcdef",
    7,
    2.5,
    -12,
    True,
    [1, "x", 3],
    {"a": 4, "x": "y", "extra": 1},
    {"a": "wrong"},
]


def validates(schema: dict[str, Any], value: Any) -> bool:
    return jsonschema.Draft202012Validator(schema).is_valid(value)


@pytest.mark.parametrize("schema", EDGE_SCHEMAS)
def test_defaults_validate(schema: dict[str, Any]) -> None:
    value = default_for(schema)
    assert validates(schema, value), value
    assert accepts(schema, value)


@pytest.mark.parametrize("schema", EDGE_SCHEMAS)
@pytest.mark.parametrize("candidate", CANDIDATES)
def test_conform_always_validates_and_accepts_agrees_with_jsonschema(
    schema: dict[str, Any], candidate: Any
) -> None:
    assert accepts(schema, candidate) == validates(schema, candidate)
    result = conform(schema, candidate)
    assert validates(schema, result), (candidate, result)
    if validates(schema, candidate) and not isinstance(candidate, dict | list | int | str):
        assert result == candidate


@pytest.mark.parametrize("call", SPEC_CALLS, ids=lambda call: call.name)
@pytest.mark.parametrize("candidate", [None, {}, {"unknown": 1}, CANDIDATES[7]])
def test_spec_schemas_conform(call: Any, candidate: Any) -> None:
    assert validates(call.schema, conform(call.schema, candidate))
    assert validates(call.schema, default_for(call.schema))


def test_default_rules() -> None:
    assert default_for({"type": "string", "minLength": 3}) == "xxx"
    assert default_for({"type": ["integer", "null"], "minimum": 4}) is None
    assert default_for({"type": "integer", "minimum": 4}) == 4
    assert default_for({"type": "integer", "maximum": -2}) == -2
    assert default_for({"type": "number", "minimum": -1, "maximum": 1}) == 0
    assert default_for({"type": "boolean"}) is False
    assert default_for({"enum": ["x", "y"]}) == "x"
    assert default_for({"type": "array", "minItems": 1, "items": {"type": "integer", "minimum": 1}}) == [1]
    assert default_for(
        {"type": "object", "properties": {"a": {"type": "string"}, "b": {}}, "required": ["a"]}
    ) == {"a": ""}
    assert default_for(False) is None  # type: ignore[arg-type]
    assert default_for({}) is None
    assert default_for(
        {"type": "object", "properties": {"n": {"$ref": "#/$defs/gone"}}, "required": ["n"]}
    ) == {"n": None}


def test_conform_keeps_valid_parts_and_fixes_the_rest() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["score", "reason", "tags", "name"],
        "properties": {
            "score": {"type": "integer", "minimum": 0, "maximum": 10},
            "reason": {"type": "string", "maxLength": 5},
            "tags": {"type": "array", "maxItems": 2, "items": {"type": "string"}},
            "name": {"type": ["string", "null"]},
        },
    }
    candidate = {"score": 80, "reason": "too long text", "tags": ["a", 1, "c"], "extra": True}
    assert conform(schema, candidate) == {"score": 10, "reason": "too l", "tags": ["a", ""], "name": None}
    assert conform(schema, {"score": 4.0, "reason": "ok", "tags": [], "name": "D"})["score"] == 4
    assert conform({"type": "integer"}, 2.5) == 0
    assert conform({"anyOf": [{"type": "integer", "maximum": 3}, {"type": "string"}]}, 9) == 3


def test_introspection() -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"$ref": "#/$defs/A"}, "b": {"anyOf": [{"type": "null"}, {"type": "number"}]}},
        "$defs": {"A": {"enum": ["x"]}},
    }
    props = property_schemas(schema)
    assert props["a"] == {"enum": ["x"]}
    assert value_type(props["a"]) == "string"
    assert value_type(props["b"]) == "number"
    assert value_type({"items": {}}) == "array"
    assert value_type({}) is None
    assert property_schemas({"type": "string"}) == {}
