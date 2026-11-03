"""Webhook ``responsePathways`` operators (specs/11 §3.2.1, Decision P07-T07)."""

from __future__ import annotations

from typing import Any

import pytest

from va_engine.handlers import evaluate, is_empty
from va_engine.models.nodes import WEBHOOK_OPERATORS


@pytest.mark.parametrize(
    ("op", "actual", "expected", "holds"),
    [
        ("==", "active", "active", True),
        ("==", " Active ", "active", True),
        ("==", "active", "inactive", False),
        ("==", 5, "5", True),
        ("==", "5.0", 5, True),
        ("==", 5, 6, False),
        ("==", True, "true", True),
        ("==", True, 1, False),
        ("==", None, None, True),
        ("==", None, "", False),
        ("==", "x", None, False),
        ("==", [1, 2], [1, 2], True),
        ("!=", "a", "b", True),
        ("!=", "a", "A", False),
        ("!=", None, "a", True),
        (">", 10, 5, True),
        (">", "10", "9", True),
        (">", 5, 5, False),
        (">", "ten", 5, False),
        (">", True, 0, False),
        (">", None, 0, False),
        (">", float("inf"), 0, False),
        (">", [1], 0, False),
        (">=", 5, 5, True),
        ("<", 4, 5.5, True),
        ("<", 6, 5.5, False),
        ("<=", 5.5, 5.5, True),
        ("<=", 5, "abc", False),
        ("contains", ["gold", "silver"], "Gold", True),
        ("contains", [1, 2, 3], "2", True),
        ("contains", ["a"], "b", False),
        ("contains", "Premium Plan", "premium", True),
        ("contains", "Basic", "premium", False),
        ("contains", "Basic", None, False),
        ("contains", {"vip": True}, "vip", True),
        ("contains", {"vip": True}, "gold", False),
        ("contains", {"vip": True}, 1, False),
        ("contains", None, "x", False),
        ("contains", 123, "2", False),
        ("not_contains", ["a"], "b", True),
        ("not_contains", "abc", "B", False),
        ("not_contains", None, "x", True),
        ("is_empty", None, None, True),
        ("is_empty", "   ", None, True),
        ("is_empty", [], None, True),
        ("is_empty", {}, None, True),
        ("is_empty", 0, None, False),
        ("is_empty", False, None, False),
        ("is_empty", "x", None, False),
        ("is_not_empty", "x", None, True),
        ("is_not_empty", [], None, False),
    ],
)
def test_operator_semantics(op: Any, actual: Any, expected: Any, holds: bool) -> None:
    assert evaluate(op, actual, expected) is holds


def test_every_operator_of_the_schema_is_evaluated() -> None:
    for op in WEBHOOK_OPERATORS:
        assert isinstance(evaluate(op, "1", "1"), bool)  # type: ignore[arg-type]


@pytest.mark.parametrize(("value", "empty"), [(None, True), ("", True), ("a", False), ([0], False)])
def test_is_empty(value: Any, empty: bool) -> None:
    assert is_empty(value) is empty
