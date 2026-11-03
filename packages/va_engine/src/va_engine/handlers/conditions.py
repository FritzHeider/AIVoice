"""Webhook ``responsePathways`` operators (specs/11 §3.2.1; Decision P07-T07 under specs/11 §4).

Pure comparisons of a variable's value (``None`` when missing) with the configured value:

* ``==`` / ``!=``: numeric comparison when both sides are numbers or numeric strings (booleans are never
  numbers), otherwise text comparison, case-insensitive and stripped (``True`` reads ``"true"``).
* ``>`` ``>=`` ``<`` ``<=``: numeric only; a side that is not numeric makes the condition false.
* ``contains`` / ``not_contains``: a list contains an element equal (``==`` rules) to the value; text contains
  the value as a case-insensitive substring; an object contains the key; anything else contains nothing.
* ``is_empty``: ``None``, blank text, ``[]`` or ``{}``; ``is_not_empty`` is its negation.
"""

from __future__ import annotations

import math
import operator
from collections.abc import Callable, Mapping
from typing import Final

from pydantic import JsonValue

from va_engine.models.nodes import WebhookOperator
from va_engine.templating import stringify

__all__ = ["evaluate", "is_empty"]

_ORDERING: Final[Mapping[str, Callable[[float, float], bool]]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}


def _number(value: JsonValue) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _text(value: JsonValue) -> str:
    return stringify(value).strip().casefold()


def _equal(actual: JsonValue, expected: JsonValue) -> bool:
    left, right = _number(actual), _number(expected)
    if left is not None and right is not None:
        return left == right
    if actual is None or expected is None:
        return actual is None and expected is None
    return _text(actual) == _text(expected)


def is_empty(value: JsonValue) -> bool:
    """``None``, blank text, an empty list or an empty object."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list | dict):
        return not value
    return False


def _contains(actual: JsonValue, expected: JsonValue) -> bool:
    if isinstance(actual, list):
        return any(_equal(item, expected) for item in actual)
    if isinstance(actual, str):
        return expected is not None and _text(expected) in actual.casefold()
    if isinstance(actual, dict):
        return isinstance(expected, str) and expected in actual
    return False


def _ordered(op: WebhookOperator, actual: JsonValue, expected: JsonValue) -> bool:
    left, right = _number(actual), _number(expected)
    if left is None or right is None:
        return False
    return _ORDERING[op](left, right)


_OPERATORS: Final[Mapping[str, Callable[[JsonValue, JsonValue], bool]]] = {
    "==": _equal,
    "!=": lambda actual, expected: not _equal(actual, expected),
    "contains": _contains,
    "not_contains": lambda actual, expected: not _contains(actual, expected),
    "is_empty": lambda actual, _expected: is_empty(actual),
    "is_not_empty": lambda actual, _expected: not is_empty(actual),
}


def evaluate(op: WebhookOperator, actual: JsonValue, expected: JsonValue) -> bool:
    """Whether ``actual <op> expected`` holds."""
    special = _OPERATORS.get(op)
    if special is not None:
        return special(actual, expected)
    return _ordered(op, actual, expected)
