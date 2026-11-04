"""Pure assertion rules and case states of the unit-test runner (specs/12 §8 Decision P07-T11a)."""

from __future__ import annotations

import pytest
from pathways_helpers import GREETING, PATHWAY, bella_pasta, spec
from pydantic import JsonValue, ValidationError

from va_pathways.unit_tests import (
    InvalidHistoryError,
    UnitTestSpec,
    case_state,
    route_matches,
    values_match,
    variables_match,
)


@pytest.mark.parametrize(
    ("expected", "actual", "matches"),
    [
        ("large", " LARGE ", True),
        ("large", "small", False),
        ("25", 25, False),
        (25, 25.0, True),
        (True, 1, False),
        (1, True, False),
        (False, False, True),
        (None, None, True),
        (None, "", False),
        ({"size": "Large"}, {"size": "large", "extra": 1}, True),
        ({"size": "Large", "crust": "thin"}, {"size": "large"}, False),
        ({"a": {"b": "X"}}, {"a": {"b": "x", "c": 2}}, True),
        ({"a": 1}, [1], False),
        (["A", 2], ["a", 2], True),
        (["A"], ["a", "b"], False),
        ([{"k": "V"}], [{"k": "v", "z": 0}], True),
        ("x", None, False),
    ],
)
def test_values_match(expected: JsonValue, actual: JsonValue, matches: bool) -> None:
    assert values_match(expected, actual) is matches


def test_variables_match_is_a_subset_check() -> None:
    actual: dict[str, JsonValue] = {"pizza_size": "Large", "eta": 25}
    assert variables_match(None, actual)
    assert variables_match({}, actual)
    assert variables_match({"pizza_size": "large"}, actual)
    assert not variables_match({"missing": None}, actual)
    assert not variables_match({"eta": "25"}, actual)


def test_route_matches() -> None:
    assert route_matches(None, "order")
    assert route_matches("order", "order")
    assert not route_matches("order", "greet")


def test_case_state_places_the_history_on_the_node() -> None:
    test = spec(
        history=[
            {"role": "assistant", "content": GREETING},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "How can I help?"},
        ]
    )
    state = case_state(test, bella_pasta())
    assert (state.runner, state.pathway_id, state.pathway_version, state.current_node_id) == (
        "pathway",
        PATHWAY,
        1,
        "greet",
    )
    assert (state.visit_counts, state.turn_index) == ({"greet": 1}, 1)
    assert [(turn.role, turn.content, turn.node_id, turn.at_ms) for turn in state.history] == [
        ("assistant", GREETING, "greet", 0),
        ("user", "Hi", "greet", 1),
        ("assistant", "How can I help?", "greet", 2),
    ]
    with pytest.raises(InvalidHistoryError) as bad:
        case_state(spec(history=[{"role": "system", "content": "x"}]), bella_pasta())
    assert bad.value.role == "system"


def test_spec_bounds_and_defaults() -> None:
    test = UnitTestSpec.model_validate({"name": "n", "node_id": "greet", "user_message": "Hi"})
    assert (test.history, test.simulated_variations, test.expected.mock_tools) == ([], 0, {})
    assert test.expected.request_data == {}
    for body in (
        {"name": "n", "node_id": "greet", "user_message": ""},
        {"name": "n", "node_id": "greet", "user_message": "Hi", "simulated_variations": 21},
        {"name": "n", "node_id": "greet", "user_message": "Hi", "simulated_variations": -1},
        {
            "name": "n",
            "node_id": "greet",
            "user_message": "Hi",
            "expected": {"mock_tools": {"h": {"status": 700}}},
        },
    ):
        with pytest.raises(ValidationError):
            UnitTestSpec.model_validate(body)
