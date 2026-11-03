"""Structured outputs: the forced-function registry, tool schemas, and output validation
(specs/10-llm-gateway.md §3; Decision 2026-11-01 P03-T14a rules 2-3 and 6-8).

Pure functions only: :meth:`va_llm.LlmGateway.call_structured` (``structured_calls``) sends the requests.
"""

from __future__ import annotations

import copy
import functools
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

import orjson
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError

__all__ = [
    "MAX_ERRORS",
    "MAX_ERROR_CHARS",
    "SCHEMA_CACHE_SIZE",
    "TOOL_NAMES",
    "InvalidOutputError",
    "check_function",
    "first_json_object",
    "json_mode_instruction",
    "register_structured_function",
    "registered_schema",
    "repair_message",
    "tool_schema",
    "validate_arguments",
]

TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "select_path",
        "score_condition",
        "extract_variables",
        "classify_answer",
        "evaluate_guard_rails",
        "grade",
        "summarize",
        "classify_disposition",
        "extract_analysis_schema",
        "answer_questions",
        "record_sentiment",
        "record_analysis",
        "extract_citations",
        "update_memory",
    }
)

MAX_ERRORS: Final = 5
# Bounded: the pathway engine builds one schema class per graph option set (specs/11 §5.5 Decision P07-T04).
SCHEMA_CACHE_SIZE: Final = 1024
MAX_ERROR_CHARS: Final = 200
_DEFS_PREFIX: Final = "#/$defs/"
_JSON_MODE_PREFIX: Final = "Respond with only a JSON object that matches this JSON Schema: "

# JSON Schema keywords whose value is one schema, a list of schemas, or a name → schema mapping. Every other
# keyword (``enum``, ``const``, ``default``, ``examples``, ...) holds data and is copied as is.
_SCHEMA_KEYWORDS: Final = frozenset(
    {
        "items",
        "additionalProperties",
        "not",
        "contains",
        "if",
        "then",
        "else",
        "propertyNames",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
_SCHEMA_LIST_KEYWORDS: Final = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SCHEMA_MAP_KEYWORDS: Final = frozenset({"properties", "patternProperties", "dependentSchemas"})

_registry: dict[str, type[BaseModel]] = {}


class InvalidOutputError(Exception):
    """A structured output that failed validation; ``errors`` are rendered ``"<path>: <message>"`` lines."""

    def __init__(self, errors: Sequence[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = tuple(errors)


def register_structured_function(name: str, schema: type[BaseModel]) -> None:
    """Register the schema of the forced function ``name`` (once, at import time of the owning module).

    Raises ``ValueError`` for a name outside :data:`TOOL_NAMES` or a second registration with another class
    (registering the same class again is a no-op).
    """
    check_function(name, schema)
    _registry[name] = schema


def registered_schema(name: str) -> type[BaseModel] | None:
    """The schema registered for ``name`` (``None`` when nothing registered it yet)."""
    return _registry.get(name)


def check_function(name: str, schema: type[BaseModel]) -> None:
    """``ValueError`` unless ``name`` is in :data:`TOOL_NAMES` and not registered with another class."""
    if name not in TOOL_NAMES:
        msg = f"{name!r} is not a registered structured function name"
        raise ValueError(msg)
    current = _registry.get(name)
    if current is not None and current is not schema:
        msg = f"{name!r} is registered with {current.__qualname__}, not {schema.__qualname__}"
        raise ValueError(msg)


def tool_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """The function ``parameters`` of ``schema``: its JSON Schema with every ``$defs`` reference inlined and
    ``additionalProperties: false`` on every object with ``properties`` that does not set it. A fresh copy per
    call; ``ValueError("recursive schema")`` for a self-referencing model."""
    return copy.deepcopy(_tool_schema(schema))


@functools.lru_cache(maxsize=SCHEMA_CACHE_SIZE)
def _tool_schema(schema: type[BaseModel]) -> dict[str, Any]:
    document = schema.model_json_schema()
    defs: dict[str, Any] = document.get("$defs", {})
    inlined: dict[str, Any] = _inline_schema(document, defs, frozenset())
    return inlined


@functools.lru_cache(maxsize=SCHEMA_CACHE_SIZE)
def _validator(schema: type[BaseModel]) -> Draft202012Validator:
    return Draft202012Validator(_tool_schema(schema))


def _inline_schema(node: Any, defs: Mapping[str, Any], seen: frozenset[str]) -> Any:
    """One schema with its references inlined (``seen``: the definitions being expanded above it)."""
    if not isinstance(node, dict):
        return copy.deepcopy(node)  # boolean schemas
    ref = node.get("$ref")
    if isinstance(ref, str):
        return _inline_reference(ref, node, defs, seen)
    inlined = {key: _inline_keyword(key, value, defs, seen) for key, value in node.items() if key != "$defs"}
    if "properties" in inlined and "additionalProperties" not in inlined:
        inlined["additionalProperties"] = False
    return inlined


def _inline_reference(
    ref: str, node: dict[str, Any], defs: Mapping[str, Any], seen: frozenset[str]
) -> dict[str, Any]:
    """The definition ``ref`` points at, inlined, with the sibling keys of ``node`` merged over it."""
    name = ref.removeprefix(_DEFS_PREFIX)
    if not ref.startswith(_DEFS_PREFIX) or name not in defs:
        msg = f"unsupported $ref {ref!r}"
        raise ValueError(msg)
    if name in seen:
        msg = "recursive schema"
        raise ValueError(msg)
    target: dict[str, Any] = _inline_schema(defs[name], defs, seen | {name})
    siblings = {key: value for key, value in node.items() if key != "$ref"}
    return {**target, **_inline_schema(siblings, defs, seen)}


def _inline_keyword(key: str, value: Any, defs: Mapping[str, Any], seen: frozenset[str]) -> Any:
    if key in _SCHEMA_KEYWORDS:
        return _inline_schema(value, defs, seen)
    if key in _SCHEMA_LIST_KEYWORDS:
        return [_inline_schema(item, defs, seen) for item in value]
    if key in _SCHEMA_MAP_KEYWORDS:
        return {name: _inline_schema(item, defs, seen) for name, item in value.items()}
    return copy.deepcopy(value)


def json_mode_instruction(schema: type[BaseModel]) -> str:
    """The system instruction of JSON mode (specs/10 §3 step 4) with the compact tool schema."""
    return _JSON_MODE_PREFIX + orjson.dumps(_tool_schema(schema)).decode()


def repair_message(function_name: str, errors: Sequence[str]) -> str:
    """The user message of the repair request (specs/10 §3 step 5)."""
    return (
        f"Your previous output was invalid: {'; '.join(errors)}. "
        f"Call {function_name} again with corrected arguments."
    )


def first_json_object(content: str | None) -> str | None:
    """The text of the first ``{...}`` in ``content`` that decodes to a JSON object (``None``: none does)."""
    if not content:
        return None
    decoder = json.JSONDecoder()
    start = content.find("{")
    while start != -1:
        try:
            _, end = decoder.raw_decode(content, start)  # a value starting with "{" is an object
        except json.JSONDecodeError:
            pass
        else:
            return content[start:end]
        start = content.find("{", start + 1)
    return None


def validate_arguments[M: BaseModel](schema: type[M], text: str) -> M:
    """``text`` parsed as JSON, validated against :func:`tool_schema` and then by ``schema``; raises
    :class:`InvalidOutputError` with at most :data:`MAX_ERRORS` rendered errors."""
    try:
        document = orjson.loads(text)
    except orjson.JSONDecodeError as exc:
        raise InvalidOutputError([_render((), f"arguments are not valid JSON ({exc.msg})")]) from exc
    if not isinstance(document, dict):
        raise InvalidOutputError([_render((), "arguments are not a JSON object")])
    violations = sorted(
        ((_path(error.absolute_path), error.message) for error in _validator(schema).iter_errors(document)),
    )
    if violations:
        raise InvalidOutputError(_capped(f"{path}: {_clip(message)}" for path, message in violations))
    try:
        return schema.model_validate(document)
    except ValidationError as exc:
        raise InvalidOutputError(
            _capped(_render(tuple(error["loc"]), error["msg"]) for error in exc.errors())
        ) from exc


def _capped(errors: Iterable[str]) -> list[str]:
    return list(errors)[:MAX_ERRORS]


def _clip(message: str) -> str:
    return message if len(message) <= MAX_ERROR_CHARS else message[: MAX_ERROR_CHARS - 1] + "…"


def _render(location: Sequence[str | int], message: str) -> str:
    return f"{_path(location)}: {_clip(message)}"


def _path(location: Iterable[str | int]) -> str:
    """``items[0].name`` style; ``$`` for the root object."""
    text = ""
    for part in location:
        if isinstance(part, int):
            text += f"[{part}]"
        else:
            text += f".{part}" if text else part
    return text or "$"
