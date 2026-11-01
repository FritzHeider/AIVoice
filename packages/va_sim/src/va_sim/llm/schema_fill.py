"""Schema-valid tool arguments (specs/30 §3.4 Decision P03-T06b rule 2).

Pure. :func:`conform` turns a heuristic's candidate into a value the request's tool ``parameters`` schema
accepts: valid parts are kept (numbers clamped into their bounds, strings cut to ``maxLength``), invalid parts
are replaced by the schema default, unknown keys are dropped when ``additionalProperties`` is false, missing
``required`` keys are filled. :func:`default_for` is the schema default. The supported keyword subset:
``type`` (string or list), ``enum``, ``const``, ``default``,
``minimum``/``maximum``/``exclusiveMinimum``/``exclusiveMaximum``, ``minLength``/``maxLength``, ``pattern``,
``minItems``/``maxItems``, ``items``, ``prefixItems``, ``properties``, ``required``, ``additionalProperties``,
``anyOf``/``oneOf``/``allOf``, and local ``$ref`` (``#/$defs/...``, ``#/definitions/...``).
"""

from __future__ import annotations

import copy
import math
import operator
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

__all__ = [
    "JsonSchema",
    "SchemaFill",
    "accepts",
    "conform",
    "default_for",
    "property_schemas",
    "value_type",
]

type JsonSchema = Mapping[str, Any]

_MAX_DEPTH: Final = 24
_EMPTY: Final[JsonSchema] = {}
_NOTHING: Final[JsonSchema] = {"not": {}}
_UNSET: Final = object()
_INFERRED: Final = (
    ("properties", "object"),
    ("required", "object"),
    ("items", "array"),
    ("prefixItems", "array"),
)


def _same(left: Any, right: Any) -> bool:
    """JSON equality: booleans never equal numbers."""
    if isinstance(left, list | dict) and type(left) is type(right):
        return _same_container(left, right)
    if _is_number(left) and _is_number(right):
        return bool(left == right)
    return type(left) is type(right) and bool(left == right)


def _same_container(left: list[Any] | dict[str, Any], right: Any) -> bool:
    if isinstance(left, dict):
        return left.keys() == right.keys() and _same_items(
            [left[key] for key in left], [right[key] for key in left]
        )
    return _same_items(left, right)


def _same_items(left: list[Any], right: list[Any]) -> bool:
    return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right, strict=True))


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


_TYPE_CHECKS: Final[Mapping[str, Callable[[Any], bool]]] = {
    "null": lambda value: value is None,
    "boolean": lambda value: isinstance(value, bool),
    "string": lambda value: isinstance(value, str),
    "number": _is_number,
    "integer": lambda value: _is_number(value) and float(value).is_integer(),
    "array": lambda value: isinstance(value, list),
    "object": lambda value: isinstance(value, dict),
}


def _types(schema: JsonSchema) -> tuple[str, ...] | None:
    declared = schema.get("type")
    if isinstance(declared, str):
        return (declared,)
    if isinstance(declared, list):
        return tuple(name for name in declared if isinstance(name, str))
    return None


def _bounds(schema: JsonSchema, *, integer: bool) -> tuple[float | None, float | None]:
    """The inclusive ``(low, high)`` of a number: an exclusive bound steps inside by one unit, or by half the
    gap to the other bound when that is closer."""
    low, high = _number_at(schema, "minimum"), _number_at(schema, "maximum")
    ex_low, ex_high = _number_at(schema, "exclusiveMinimum"), _number_at(schema, "exclusiveMaximum")
    lower, upper = _tightest(low, ex_low, max), _tightest(high, ex_high, min)
    if ex_low is not None and lower == ex_low:
        low = math.floor(ex_low) + 1 if integer else ex_low + _inset(ex_low, upper)
    if ex_high is not None and upper == ex_high:
        high = math.ceil(ex_high) - 1 if integer else ex_high - _inset(lower, ex_high)
    return low, high


def _tightest(
    inclusive: float | None, exclusive: float | None, pick: Callable[[float, float], float]
) -> float | None:
    if inclusive is None or exclusive is None:
        return exclusive if inclusive is None else inclusive
    return pick(inclusive, exclusive)


def _inset(low: float | None, high: float | None) -> float:
    """One unit, or half the open interval ``(low, high)`` when it is narrower than two units."""
    if low is None or high is None:
        return 1.0
    return min(1.0, (high - low) / 2)


def _number_at(schema: JsonSchema, key: str) -> float | None:
    value = schema.get(key)
    return value if _is_number(value) else None


def _clamp(value: float, low: float | None, high: float | None) -> float:
    if low is not None and value < low:
        value = low
    if high is not None and value > high:
        value = high
    return value


_NUMBER_CHECKS: Final[tuple[tuple[str, Callable[[Any, Any], bool]], ...]] = (
    ("minimum", operator.ge),
    ("maximum", operator.le),
    ("exclusiveMinimum", operator.gt),
    ("exclusiveMaximum", operator.lt),
)


class SchemaFill:
    """Defaults, validity, and conformance against one root schema (the root resolves ``$ref``)."""

    def __init__(self, root: JsonSchema) -> None:
        self.root = root

    # -- resolution -------------------------------------------------------------------------------------

    def resolve(self, schema: Any) -> JsonSchema:
        """``schema`` with local ``$ref``s followed; ``true`` / unknown refs become ``{}``, ``false`` stays
        a schema nothing satisfies (``{"not": {}}``)."""
        for _ in range(_MAX_DEPTH):
            if schema is False:
                return _NOTHING
            if not isinstance(schema, Mapping):
                return _EMPTY
            mapping: JsonSchema = schema
            ref = mapping.get("$ref")
            if not isinstance(ref, str):
                return mapping
            schema = self._pointer(ref)
        return _EMPTY

    def _pointer(self, ref: str) -> Any:
        if not ref.startswith("#"):
            return True
        node: Any = self.root
        path = ref.removeprefix("#").strip("/")
        for part in path.split("/") if path else ():
            key = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, Mapping) or key not in node:
                return True
            node = node[key]
        return node

    def properties(self, schema: Any) -> dict[str, JsonSchema]:
        """The resolved ``properties`` of an object schema."""
        resolved = self.resolve(schema)
        props = resolved.get("properties")
        if not isinstance(props, Mapping):
            return {}
        return {str(name): self.resolve(sub) for name, sub in props.items()}

    def value_type(self, schema: Any) -> str | None:
        """The first non-null type of ``schema`` (also through ``anyOf``/``oneOf`` and ``enum`` values)."""
        resolved = self.resolve(schema)
        declared = _types(resolved) or ()
        for name in declared:
            if name != "null":
                return name
        for branch in self._branches(resolved):
            found = self.value_type(branch)
            if found is not None:
                return found
        return self._inferred_type(resolved)

    def _inferred_type(self, schema: JsonSchema) -> str | None:
        for keyword, name in _INFERRED:
            if keyword in schema:
                return name
        values = [value for value in schema.get("enum", ()) if value is not None]
        if values:
            return next(
                name for name, check in _TYPE_CHECKS.items() if name != "integer" and check(values[0])
            )
        return None

    @staticmethod
    def _branches(schema: JsonSchema) -> list[Any]:
        branches = schema.get("anyOf") or schema.get("oneOf") or []
        return list(branches) if isinstance(branches, list) else []

    # -- validity ---------------------------------------------------------------------------------------

    def accepts(self, schema: Any, value: Any, depth: int = 0) -> bool:
        """Whether ``value`` satisfies ``schema`` (the supported keywords only)."""
        resolved = self.resolve(schema)
        if depth > _MAX_DEPTH or resolved is _NOTHING:
            return False
        return (
            self._literal_ok(resolved, value)
            and self._combinators_ok(resolved, value, depth)
            and self._type_ok(resolved, value)
            and self._string_ok(resolved, value)
            and self._number_ok(resolved, value)
            and self._array_ok(resolved, value, depth)
            and self._object_ok(resolved, value, depth)
        )

    @staticmethod
    def _literal_ok(schema: JsonSchema, value: Any) -> bool:
        if "const" in schema and not _same(schema["const"], value):
            return False
        enum = schema.get("enum")
        return not isinstance(enum, list) or any(_same(option, value) for option in enum)

    def _combinators_ok(self, schema: JsonSchema, value: Any, depth: int) -> bool:
        any_of, one_of, all_of = schema.get("anyOf"), schema.get("oneOf"), schema.get("allOf")
        if isinstance(any_of, list) and not any(self.accepts(sub, value, depth + 1) for sub in any_of):
            return False
        if isinstance(one_of, list) and sum(self.accepts(sub, value, depth + 1) for sub in one_of) != 1:
            return False
        return not isinstance(all_of, list) or all(self.accepts(sub, value, depth + 1) for sub in all_of)

    @staticmethod
    def _type_ok(schema: JsonSchema, value: Any) -> bool:
        declared = _types(schema)
        return declared is None or any(_TYPE_CHECKS.get(name, lambda _v: False)(value) for name in declared)

    @staticmethod
    def _string_ok(schema: JsonSchema, value: Any) -> bool:
        if not isinstance(value, str):
            return True
        if len(value) < schema.get("minLength", 0):
            return False
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return False
        pattern = schema.get("pattern")
        return not isinstance(pattern, str) or _search(pattern, value)

    @staticmethod
    def _number_ok(schema: JsonSchema, value: Any) -> bool:
        if not _is_number(value):
            return True
        return all(
            check(value, schema[name]) for name, check in _NUMBER_CHECKS if _is_number(schema.get(name))
        )

    def _array_ok(self, schema: JsonSchema, value: Any, depth: int) -> bool:
        if not isinstance(value, list):
            return True
        if len(value) < schema.get("minItems", 0):
            return False
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return False
        return all(
            self.accepts(self._item_schema(schema, index), item, depth + 1)
            for index, item in enumerate(value)
        )

    def _item_schema(self, schema: JsonSchema, index: int) -> Any:
        prefix = schema.get("prefixItems")
        if isinstance(prefix, list) and index < len(prefix):
            return prefix[index]
        items = schema.get("items", True)
        return items if items is not None else True

    def _object_ok(self, schema: JsonSchema, value: Any, depth: int) -> bool:
        if not isinstance(value, dict):
            return True
        if any(name not in value for name in schema.get("required", ())):
            return False
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            sub = props.get(name) if isinstance(props, Mapping) and name in props else extra
            if not self.accepts(sub, item, depth + 1):
                return False
        return True

    # -- defaults ---------------------------------------------------------------------------------------

    def default(self, schema: Any, depth: int = 0) -> Any:
        """The schema default: ``default`` → ``const`` → ``enum`` (null if listed, else the first) → null
        when nullable → a branch's default → the default of the first non-null type."""
        resolved = self.resolve(schema)
        literal = _literal_default(resolved)
        if literal is not _UNSET:
            return literal
        if "null" in (_types(resolved) or ()) or depth > _MAX_DEPTH:
            return None
        merged = self._merged_all_of(resolved)
        if merged is not None:
            return self.default(merged, depth + 1)
        branches = self._branches(resolved)
        if branches:
            return self._branch_default(branches, depth)
        return self._typed_default(resolved, self.value_type(resolved), depth)

    def _merged_all_of(self, schema: JsonSchema) -> JsonSchema | None:
        """``schema`` with its ``allOf`` branches merged in (shallowly), None without ``allOf``."""
        all_of = schema.get("allOf")
        if not isinstance(all_of, list) or not all_of:
            return None
        merged = {key: value for key, value in schema.items() if key != "allOf"}
        for branch in all_of:
            merged.update(self.resolve(branch))
        return merged

    def _branch_default(self, branches: Sequence[Any], depth: int) -> Any:
        if any(self.accepts(branch, None, depth + 1) for branch in branches):
            return None
        return self.default(branches[0], depth + 1)

    def _typed_default(self, schema: JsonSchema, kind: str | None, depth: int) -> Any:
        if kind in {"integer", "number"}:
            if kind == "number":
                return _clamp(0, *_bounds(schema, integer=False))
            low, high = _bounds(schema, integer=True)
            low = None if low is None else math.ceil(low)
            high = None if high is None else math.floor(high)
            return int(_clamp(0, low, high))
        builders: Mapping[str | None, Callable[[], Any]] = {
            "string": lambda: _string_default(schema),
            "boolean": lambda: False,
            "array": lambda: [
                self.default(self._item_schema(schema, index), depth + 1)
                for index in range(schema.get("minItems", 0))
            ],
            "object": lambda: self._object_default(schema, depth),
        }
        builder = builders.get(kind)
        return builder() if builder is not None else None

    def _object_default(self, schema: JsonSchema, depth: int) -> dict[str, Any]:
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        return {
            name: self.default(props.get(name, extra) if isinstance(props, Mapping) else extra, depth + 1)
            for name in schema.get("required", ())
        }

    # -- conformance ------------------------------------------------------------------------------------

    def conform(self, schema: Any, candidate: Any, depth: int = 0) -> Any:
        """``candidate`` made valid against ``schema`` (see the module docstring)."""
        resolved = self.resolve(schema)
        if depth > _MAX_DEPTH:
            return self.default(resolved, depth)
        shaped = self._shape(resolved, candidate, depth)
        if self.accepts(resolved, shaped, depth):
            return shaped
        for branch in self._branches(resolved):
            reshaped = self._shape(self.resolve(branch), candidate, depth)
            if self.accepts(resolved, reshaped, depth):
                return reshaped
        return self.default(resolved, depth)

    def _shape(self, schema: JsonSchema, candidate: Any, depth: int) -> Any:
        """``candidate`` with its parts fixed for the container/number/string keywords of ``schema``."""
        declared = _types(schema)

        def allows(name: str) -> bool:
            return declared is None or name in declared

        if isinstance(candidate, dict) and allows("object"):
            return self._shape_object(schema, candidate, depth)
        if isinstance(candidate, list) and allows("array"):
            return self._shape_array(schema, candidate, depth)
        if _is_number(candidate) and (allows("number") or allows("integer")):
            return _shape_number(schema, candidate, integer=not allows("number"))
        if isinstance(candidate, str) and "maxLength" in schema:
            return candidate[: schema["maxLength"]]
        return candidate

    def _shape_object(self, schema: JsonSchema, candidate: Mapping[str, Any], depth: int) -> dict[str, Any]:
        props = schema.get("properties", {})
        props = props if isinstance(props, Mapping) else {}
        extra = schema.get("additionalProperties", True)
        shaped: dict[str, Any] = {}
        for name, value in candidate.items():
            if name in props:
                shaped[name] = self.conform(props[name], value, depth + 1)
            elif extra is not False:
                shaped[name] = self.conform(extra, value, depth + 1)
        for name in schema.get("required", ()):
            if name not in shaped:
                shaped[name] = self.default(props.get(name, extra), depth + 1)
        return shaped

    def _shape_array(self, schema: JsonSchema, candidate: Sequence[Any], depth: int) -> list[Any]:
        limit = schema.get("maxItems", len(candidate))
        shaped = [
            self.conform(self._item_schema(schema, index), item, depth + 1)
            for index, item in enumerate(candidate[:limit])
        ]
        for index in range(len(shaped), schema.get("minItems", 0)):
            shaped.append(self.default(self._item_schema(schema, index), depth + 1))
        return shaped


def _literal_default(schema: JsonSchema) -> Any:
    """``default``, else ``const``, else null when the enum lists it or the first enum value, else unset."""
    if "default" in schema:
        return copy.deepcopy(schema["default"])
    if "const" in schema:
        return copy.deepcopy(schema["const"])
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return None if None in enum else copy.deepcopy(enum[0])
    return _UNSET


def _search(pattern: str, value: str) -> bool:
    try:
        return re.search(pattern, value) is not None
    except re.error:
        return True


def _string_default(schema: JsonSchema) -> str:
    examples = schema.get("examples")
    if isinstance(examples, list) and examples and isinstance(examples[0], str):
        return examples[0]
    return "x" * int(schema.get("minLength", 0))


def _shape_number(schema: JsonSchema, value: float, *, integer: bool) -> int | float:
    low, high = _bounds(schema, integer=integer)
    clamped = _clamp(value, low, high)
    if integer and float(clamped).is_integer():
        return int(clamped)
    return clamped


def accepts(schema: JsonSchema, value: Any) -> bool:
    return SchemaFill(schema).accepts(schema, value)


def default_for(schema: JsonSchema) -> Any:
    return SchemaFill(schema).default(schema)


def conform(schema: JsonSchema, candidate: Any) -> Any:
    return SchemaFill(schema).conform(schema, candidate)


def property_schemas(schema: JsonSchema) -> dict[str, JsonSchema]:
    return SchemaFill(schema).properties(schema)


def value_type(schema: JsonSchema) -> str | None:
    return SchemaFill(schema).value_type(schema)
