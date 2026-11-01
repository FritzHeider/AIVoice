"""Shared pieces of the FakeLLM structured-call heuristics (specs/30 §3.4 Decision P03-T06b rule 4).

:class:`Heuristic` bundles the request's tool schema and the text sources a heuristic reads; the value helpers
extract one typed value (integer, number, boolean, string, enum) from text.
"""

from __future__ import annotations

import re
from functools import cached_property
from typing import Any, Final

from va_sim.llm.params import ChatRequest
from va_sim.llm.schema_fill import JsonSchema, SchemaFill
from va_sim.llm.text import (
    caller_text,
    first_integer,
    first_number,
    first_words,
    latest_caller,
    name_value,
    overlap,
    transcript_text,
    trimmed,
    yes_no,
)

__all__ = [
    "Heuristic",
    "analysis_value",
    "enum_choice",
    "enum_values",
    "string_value",
    "summary_text",
    "typed_value",
]

_EMAIL: Final = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE: Final = re.compile(r"\+?\d[\d\s().-]{6,}\d")
_DATE: Final = re.compile(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?")
_LATEST_LIMIT: Final = 80
_SUMMARY_WORDS: Final = 30


class Heuristic:
    """One forced call: the tool's ``parameters`` (through :class:`SchemaFill`) and the request's text."""

    def __init__(self, fill: SchemaFill, request: ChatRequest) -> None:
        self.fill = fill
        self.request = request

    @cached_property
    def properties(self) -> dict[str, JsonSchema]:
        return self.fill.properties(self.fill.root)

    @cached_property
    def latest(self) -> str:
        return latest_caller(self.request)

    @cached_property
    def caller(self) -> str:
        return caller_text(self.request)

    @cached_property
    def transcript(self) -> str:
        return transcript_text(self.request)

    def enum_of(self, name: str, schema: Any = None) -> list[Any]:
        """The non-null ``enum`` values of property ``name`` (of ``schema``, default the parameters)."""
        props = self.properties if schema is None else self.fill.properties(schema)
        return enum_values(self.fill, props.get(name, {}))


def enum_values(fill: SchemaFill, schema: Any) -> list[Any]:
    """The non-null ``enum`` values of ``schema`` (also inside ``anyOf``/``oneOf`` branches)."""
    resolved = fill.resolve(schema)
    enum = resolved.get("enum")
    if isinstance(enum, list):
        return [value for value in enum if value is not None]
    for key in ("anyOf", "oneOf"):
        for branch in resolved.get(key) or ():
            values = enum_values(fill, branch)
            if values:
                return values
    return []


def typed_value(kind: str | None, text: str, latest: str) -> Any:
    """``integer``/``number``/``boolean`` from ``text``; ``string`` = a stated name, else ``latest`` cut to 80
    characters; anything else None."""
    if kind == "integer":
        return first_integer(text)
    if kind == "number":
        return first_number(text)
    if kind == "boolean":
        return yes_no(text)
    if kind == "string":
        return name_value(text) or trimmed(latest, _LATEST_LIMIT) or None
    return None


def enum_choice(options: list[Any], text: str) -> Any:
    """The option whose words overlap ``text`` most (ties: first), None when nothing overlaps."""
    best, chosen = 0, None
    for option in options:
        score = overlap(str(option).replace("_", " "), text)
        if score > best:
            best, chosen = score, option
    return chosen


def string_value(name: str, schema: JsonSchema, text: str) -> str | None:
    """An email, phone number, or date when the field asks for one, else a stated name, else None."""
    description = str(schema.get("description", ""))
    hint = f"{name} {description}".lower()
    pattern = None
    if "email" in hint:
        pattern = _EMAIL
    elif "phone" in hint:
        pattern = _PHONE
    elif description.startswith("YYYY"):
        pattern = _DATE
    if pattern is not None:
        match = pattern.search(text)
        return match.group(0).strip() if match is not None else None
    return name_value(text)


def analysis_value(heuristic: Heuristic, name: str, schema: JsonSchema, text: str) -> Any:
    """One analysis field from ``text`` by its type (enums by overlap)."""
    options = enum_values(heuristic.fill, schema)
    if options:
        return enum_choice(options, text)
    kind = heuristic.fill.value_type(schema)
    if kind == "string":
        return string_value(name, heuristic.fill.resolve(schema), text)
    return typed_value(kind, text, "") if kind in {"integer", "number", "boolean"} else None


def summary_text(transcript: str) -> str:
    """``Summary: `` + the first 30 words of ``transcript``."""
    return "Summary: " + first_words(transcript, _SUMMARY_WORDS)
