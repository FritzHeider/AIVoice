"""Variable extraction (specs/11 §5.3; specs/10 §9.3; specs/11 §5.5 Decision P07-T04 rules 5 and 6)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from decision_helpers import CALL_ID, NOW, ORG_ID, ScriptedPort, inputs, invalid, unavailable
from graph_helpers import graph, start
from hypothesis import given
from hypothesis import strategies as st
from service_helpers import json_log_stream, log_events

from va_engine.decision_schemas import VariableType
from va_engine.extraction import (
    REJECTED,
    ExtractionResult,
    Extractor,
    coerce_value,
    now_local_text,
    render_extraction_prompt,
)
from va_engine.models.nodes import PathwayNode

VARS = [
    {"name": "first_name", "type": "string", "description": "Caller's first name"},
    {"name": "party_size", "type": "integer", "description": "How many\npeople"},
    {"name": "budget", "type": "number"},
    {"name": "is_member", "type": "boolean", "description": "Loyalty member"},
]


def extract_node(variables: list[dict[str, Any]] | None = None) -> PathwayNode:
    return graph([start("book", name="Book", extractVars=VARS if variables is None else variables)]).nodes[0]


async def extract(
    node: PathwayNode, *script: dict[str, Any] | Exception, timezone: str = "America/New_York"
) -> tuple[ExtractionResult | None, ScriptedPort]:
    port = ScriptedPort(list(script))
    result = await Extractor(port, clock=port.clock).extract(
        node, inputs("four of us", ("assistant", "How many?")), now=NOW, timezone=timezone
    )
    return result, port


@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    [
        ("string", "  Mike ", "Mike"),
        ("string", "   ", None),
        ("string", None, None),
        ("integer", 4, 4),
        ("integer", 3.0, 3),
        ("integer", 3.5, REJECTED),
        ("integer", "1,200", 1200),
        ("integer", "twenty five", 25),
        ("integer", "a hundred and five", 105),
        ("integer", "two thousand.", 2000),
        ("integer", "3.0", 3),
        ("integer", "-7", -7),
        ("integer", "a few", REJECTED),
        ("integer", "123456789012345678", 123456789012345678),
        ("integer", "12345678901234567890", REJECTED),
        ("integer", "1" * 5000, REJECTED),
        ("integer", "1,000" * 1700, REJECTED),
        ("integer", 2**63, REJECTED),
        ("integer", -(2**63), -(2**63)),
        ("integer", 1e300, REJECTED),
        ("number", "1" * 5000, REJECTED),
        ("number", "1" * 5000 + ".5", REJECTED),
        ("number", "9" * 4400 + " thousand", REJECTED),
        ("number", 2.5, 2.5),
        ("number", 2.0, 2),
        ("number", 7, 7),
        ("number", "1,234.50", 1234.5),
        ("number", ".5", 0.5),
        ("number", "eleven", 11),
        ("number", "lots", REJECTED),
        ("boolean", True, True),
        ("boolean", False, False),
        ("boolean", "Yeah, sure!", True),
        ("boolean", "nope", False),
        ("boolean", "No, not really", False),
        ("boolean", "yes no", REJECTED),
        ("boolean", "maybe", REJECTED),
        ("boolean", "", None),
        ("boolean", "?!", REJECTED),
        ("boolean", 1, REJECTED),
        ("string", 5, REJECTED),
        ("integer", True, REJECTED),
        ("integer", float("inf"), REJECTED),
        ("number", False, REJECTED),
        ("number", float("nan"), REJECTED),
        ("number", [1], REJECTED),
    ],
)
def test_coerce_value(kind: VariableType, value: Any, expected: Any) -> None:
    coerced = coerce_value(kind, value)
    assert coerced == expected
    assert type(coerced) is type(expected)


@given(st.sampled_from(["string", "integer", "number", "boolean"]), st.text() | st.integers() | st.floats())
def test_coerce_value_never_raises(kind: VariableType, value: str | int | float) -> None:
    coerce_value(kind, value)


async def test_values_nulls_and_rejections() -> None:
    reply = {"first_name": " Mike ", "party_size": "four", "budget": None, "is_member": "maybe"}
    result, port = await extract(extract_node(), reply)
    assert result == ExtractionResult(
        values={"first_name": "Mike", "party_size": 4}, nulls=("budget",), rejected=("is_member",), ms=250
    )
    assert result.event_data() == {"variables": {"first_name": "Mike", "party_size": 4}}
    [request] = port.requests
    assert (request.call_type, request.function_name, request.org_id, request.call_id) == (
        "extraction",
        "extract_variables",
        ORG_ID,
        CALL_ID,
    )
    assert request.user == 'Agent: How many?\nLatest caller message: "four of us"'


async def test_schema_has_every_variable_required_and_nullable() -> None:
    _, port = await extract(
        extract_node(), dict.fromkeys(("first_name", "party_size", "budget", "is_member"))
    )
    parameters = port.requests[0].parameters()
    assert parameters["required"] == ["first_name", "party_size", "budget", "is_member"]
    types = {
        name: [branch["type"] for branch in prop["anyOf"]] for name, prop in parameters["properties"].items()
    }
    assert types == {
        "first_name": ["string", "null"],
        "party_size": ["integer", "string", "null"],
        "budget": ["number", "string", "null"],
        "is_member": ["boolean", "string", "null"],
    }
    assert parameters["additionalProperties"] is False


async def test_repeated_names_keep_the_first_declaration() -> None:
    variables = [{"name": "model_config", "type": "integer"}, {"name": "model_config", "type": "string"}]
    result, port = await extract(extract_node(variables), {"model_config": "nine"})
    assert result is not None
    assert result.values == {"model_config": 9}
    assert list(port.requests[0].parameters()["properties"]) == ["model_config"]


async def test_no_variables_makes_no_call() -> None:
    result, port = await extract(extract_node([]))
    assert result is None
    assert port.requests == []


@pytest.mark.parametrize("failure", [unavailable(), invalid("extract_variables")])
async def test_failure_returns_no_values(failure: Exception) -> None:
    stream = json_log_stream()
    result, _ = await extract(extract_node(), failure)
    assert result == ExtractionResult(degraded=True, ms=250)
    [event] = [e for e in log_events(stream) if e["event"] == "engine.extraction_degraded"]
    assert (event["node_id"], event["reason"]) == ("book", type(failure).__name__)


def test_prompt() -> None:
    assert render_extraction_prompt(extract_node(), now=NOW, timezone="America/New_York") == (
        "Extract the following information from the phone conversation. Use only what the caller or the "
        "agent explicitly stated. If a value was not stated, return null for it. Do not guess.\n\n"
        "Variables:\n"
        "- first_name (string): Caller's first name\n"
        "- party_size (integer): How many people\n"
        "- budget (number): \n"
        "- is_member (boolean): Loyalty member\n\n"
        "Current date and time for resolving relative dates: Tuesday 2026-11-03 09:05 (America/New_York).\n"
        "Call extract_variables exactly once."
    )


@pytest.mark.parametrize(
    ("timezone", "expected"),
    [
        ("Asia/Tokyo", ("Wednesday 2026-11-04 03:30", "Asia/Tokyo")),
        ("Not/AZone", ("Tuesday 2026-11-03 18:30", "UTC")),
        ("", ("Tuesday 2026-11-03 18:30", "UTC")),
        ("../etc", ("Tuesday 2026-11-03 18:30", "UTC")),
    ],
)
def test_now_local_text(timezone: str, expected: tuple[str, str]) -> None:
    assert now_local_text(datetime(2026, 11, 3, 18, 30, tzinfo=UTC), timezone) == expected


def test_rejected_repr() -> None:
    assert repr(REJECTED) == "REJECTED"
