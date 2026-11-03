"""Variable extraction (specs/11 §5.3; specs/10 §9.3; specs/11 §5.5 Decision P07-T04 rules 5 and 6).

:meth:`Extractor.extract` asks the router model (``extract_variables``) for a node's ``extractVars`` with a
per-node schema, then coerces each value to its declared type (:func:`coerce_value`: spoken numbers, digit
strings, yes/no words). Whether the call runs before the response or in the background, and the overwrite
merge into the variable bag, are the caller's; this module only returns an :class:`ExtractionResult`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import JsonValue

from va_common.logging import get_logger
from va_common.time import Clock
from va_engine.decision_schemas import VariableType, extraction_schema
from va_engine.decisions import (
    DecisionInputs,
    StructuredPort,
    elapsed_ms,
    latest_line,
    one_line,
    transcript_lines,
)
from va_engine.models.nodes import PathwayNode
from va_engine.spoken_numbers import MAX_DIGITS, parse_spoken_integer
from va_llm.errors import LlmUnavailableError, StructuredOutputError
from va_llm.models import ChatMessage
from va_llm.prompts import render_prompt
from va_voice.text_norm import tokens

__all__ = [
    "EXTRACTION_HISTORY",
    "NO_WORDS",
    "REJECTED",
    "YES_WORDS",
    "ExtractionResult",
    "Extractor",
    "Rejected",
    "coerce_value",
    "now_local_text",
    "render_extraction_prompt",
]

EXTRACTION_HISTORY: Final = 10
EXTRACTION_DESCRIPTION: Final = "Record the variables stated in the phone conversation."
YES_WORDS: Final = frozenset({"yes", "yeah", "yep", "yup", "true", "correct", "sure", "right"})
NO_WORDS: Final = frozenset({"no", "nope", "nah", "false", "incorrect"})
_WEEKDAYS: Final = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_INT64_MIN: Final = -(2**63)
_INT64_MAX: Final = 2**63 - 1
_NUMBER: Final = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|[+-]?\.\d+")

_logger = get_logger(__name__)


class Rejected:
    """A value that does not coerce to the variable's type (the :data:`REJECTED` singleton)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "REJECTED"


REJECTED: Final = Rejected()


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """``values`` holds the non-null coerced values (variable order); ``nulls`` the variables returned as
    null, ``rejected`` those whose value did not fit the type (also null)."""

    values: dict[str, JsonValue] = field(default_factory=dict)
    nulls: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    degraded: bool = False
    ms: int = 0

    def event_data(self) -> dict[str, JsonValue]:
        """``call.extraction`` data (specs/19 §2)."""
        return {"variables": dict(self.values)}


def _integer(value: JsonValue) -> int | Rejected:
    number = _number(value)
    return REJECTED if isinstance(number, float) else number


def _number(value: JsonValue) -> int | float | Rejected:
    if isinstance(value, bool):
        return REJECTED
    if isinstance(value, int):
        return value if _INT64_MIN <= value <= _INT64_MAX else REJECTED
    if isinstance(value, float):
        if not math.isfinite(value):
            return REJECTED
        return _number(int(value)) if value.is_integer() else value
    if isinstance(value, str):
        return _number_text(value)
    return REJECTED


def _number_text(value: str) -> int | float | Rejected:
    text = value.strip().rstrip(".!?")
    if _NUMBER.fullmatch(text):
        digits = text.replace(",", "")
        whole = digits.lstrip("+-").split(".", 1)[0]
        if len(whole) > MAX_DIGITS:
            return REJECTED
        return _number(float(digits)) if "." in digits else int(digits)
    spoken = parse_spoken_integer(text)
    return spoken if spoken is not None else REJECTED


def _boolean(value: JsonValue) -> bool | Rejected:
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return REJECTED
    words = tokens(value)
    if not words:
        return REJECTED
    rest = set(words[1:])
    if words[0] in YES_WORDS and not rest & NO_WORDS:
        return True
    if words[0] in NO_WORDS and not rest & YES_WORDS:
        return False
    return REJECTED


def coerce_value(kind: VariableType, value: JsonValue) -> JsonValue | Rejected:
    """``value`` as ``kind`` (specs/11 §5.5 Decision P07-T04 rule 5): ``None`` stays null, a blank string
    becomes null, and a value that does not fit is :data:`REJECTED`."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    match kind:
        case "string":
            return value.strip() if isinstance(value, str) else REJECTED
        case "integer":
            return _integer(value)
        case "number":
            return _number(value)
        case "boolean":
            return _boolean(value)


def now_local_text(now: datetime, timezone: str) -> tuple[str, str]:
    """``(now_local, zone)``: ``%A %Y-%m-%d %H:%M`` in ``timezone`` with English weekday names; an unknown
    zone renders in UTC."""
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone, timezone = ZoneInfo("UTC"), "UTC"
    local = now.astimezone(zone)
    return f"{_WEEKDAYS[local.weekday()]} {local:%Y-%m-%d %H:%M}", timezone


def render_extraction_prompt(node: PathwayNode, *, now: datetime, timezone: str) -> str:
    """The specs/10 §9.3 system prompt for ``node``'s ``extractVars``."""
    now_local, zone = now_local_text(now, timezone)
    lines = [f"- {var.name} ({var.type}): {one_line(var.description)}" for var in node.data.extract_vars]
    return render_prompt(
        "extract", {"variable_lines": "\n".join(lines), "now_local": now_local, "timezone": zone}
    )


class Extractor:
    """Extracts node variables over the router model."""

    def __init__(self, gateway: StructuredPort, *, clock: Clock) -> None:
        self._gateway = gateway
        self._clock = clock

    async def extract(
        self, node: PathwayNode, inputs: DecisionInputs, *, now: datetime, timezone: str
    ) -> ExtractionResult | None:
        """The extracted variables of ``node`` (``None`` when it declares none); never raises for LLM
        failures (``degraded`` result without values)."""
        variables: dict[str, VariableType] = {}
        for var in node.data.extract_vars:
            variables.setdefault(var.name, var.type)
        if not variables:
            return None
        lines = transcript_lines(inputs.history, EXTRACTION_HISTORY)
        lines.append(latest_line(inputs.latest))
        messages = [
            ChatMessage(role="system", content=render_extraction_prompt(node, now=now, timezone=timezone)),
            ChatMessage(role="user", content="\n".join(lines)),
        ]
        started = self._clock.monotonic()
        try:
            output = await self._gateway.call_structured(
                "extraction",
                extraction_schema(tuple(variables.items())),
                org_id=inputs.org_id,
                messages=messages,
                function_name="extract_variables",
                description=EXTRACTION_DESCRIPTION,
                call_id=inputs.call_id,
            )
        except (LlmUnavailableError, StructuredOutputError) as error:
            _logger.warning("engine.extraction_degraded", node_id=node.id, reason=type(error).__name__)
            return ExtractionResult(degraded=True, ms=elapsed_ms(started, self._clock.monotonic()))
        ms = elapsed_ms(started, self._clock.monotonic())
        return _result(variables, output.model_dump(by_alias=True), ms)


def _result(variables: dict[str, VariableType], raw: dict[str, JsonValue], ms: int) -> ExtractionResult:
    values: dict[str, JsonValue] = {}
    nulls: list[str] = []
    rejected: list[str] = []
    for name, kind in variables.items():
        coerced = coerce_value(kind, raw.get(name))
        if isinstance(coerced, Rejected):
            rejected.append(name)
        elif coerced is None:
            nulls.append(name)
        else:
            values[name] = coerced
    return ExtractionResult(values, tuple(nulls), tuple(rejected), ms=ms)
