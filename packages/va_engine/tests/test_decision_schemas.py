"""Decision-call output schemas (specs/10 §9.1-§9.3; specs/11 §5.5 Decision P07-T04 rule 7)."""

from __future__ import annotations

import orjson
import pytest
from jsonschema import Draft202012Validator

from va_engine.decision_schemas import ConditionScore, extraction_schema, route_schema
from va_llm.structured import InvalidOutputError, tool_schema, validate_arguments


def test_route_schema_matches_the_spec_shape() -> None:
    parameters = tool_schema(route_schema(("e1", "stay", "e1", "global:g")))
    assert parameters["required"] == ["path_id", "confidence", "alternatives", "reason"]
    assert parameters["properties"]["path_id"]["enum"] == ["e1", "stay", "global:g"]
    alternative = parameters["properties"]["alternatives"]["items"]
    assert alternative["required"] == ["path_id", "confidence"]
    assert alternative["additionalProperties"] is False
    assert parameters["properties"]["reason"]["maxLength"] == 200
    Draft202012Validator.check_schema(parameters)


@pytest.mark.parametrize(
    "arguments",
    [
        {"path_id": "nope", "confidence": 5, "alternatives": [], "reason": ""},
        {"path_id": "e1", "confidence": 11, "alternatives": [], "reason": ""},
        {"path_id": "e1", "confidence": 5, "alternatives": [{"path_id": "e1"}], "reason": ""},
        {"path_id": "e1", "confidence": 5, "alternatives": [], "reason": "x" * 201},
        {"path_id": "e1", "confidence": 5, "alternatives": []},
    ],
)
def test_route_schema_rejects_bad_outputs(arguments: dict[str, object]) -> None:
    with pytest.raises(InvalidOutputError):
        validate_arguments(route_schema(("e1", "stay")), orjson.dumps(arguments).decode())


def test_route_schema_needs_options() -> None:
    with pytest.raises(ValueError, match="at least one option"):
        route_schema(())


def test_schemas_are_cached_per_option_tuple() -> None:
    assert route_schema(("a", "stay")) is route_schema(("a", "stay"))
    assert route_schema(("a", "stay")) is not route_schema(("b", "stay"))
    variables = (("x", "integer"),)
    assert extraction_schema(variables) is extraction_schema(variables)
    assert route_schema.cache_info().maxsize == extraction_schema.cache_info().maxsize == 256


def test_extraction_schema_uses_variable_names_for_any_name() -> None:
    schema = extraction_schema((("first name", "string"), ("1st", "integer"), ("json", "boolean")))
    parsed = validate_arguments(schema, '{"first name": null, "1st": "two", "json": true}')
    assert parsed.model_dump(by_alias=True) == {"first name": None, "1st": "two", "json": True}
    with pytest.raises(InvalidOutputError):
        validate_arguments(schema, '{"first name": null, "1st": 2.5, "json": true}')
    with pytest.raises(InvalidOutputError):
        validate_arguments(schema, '{"first name": null, "1st": 2}')


def test_condition_schema() -> None:
    parameters = tool_schema(ConditionScore)
    assert parameters["required"] == ["score", "met", "missing"]
    assert parameters["properties"]["missing"]["maxLength"] == 200
