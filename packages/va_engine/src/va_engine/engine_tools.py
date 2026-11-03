"""Scheduling tool rounds of a pathway reply (specs/11 §3.2.7; Decision P07-T08b rule 8 under §5.9).

A Scheduling node's reply is offered :data:`~va_engine.handlers.SCHEDULING_FUNCTIONS`. After the reply
streamed, :func:`run_scheduling_calls` answers the model's calls in order (``list_slots`` from the stored
slots, ``book_slot`` through the node's booking request); the turn orchestration records the answers and lets
the node respond again, at most :data:`MAX_SCHEDULING_ROUNDS` rounds per pass. A failed booking that names
``onFailureNodeId`` routes there and the remaining calls of the round are skipped.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

import orjson

from va_engine.handlers import SCHEDULING_FUNCTIONS, TurnContext, book_slot, list_slots
from va_engine.handlers.reports import NodeReport
from va_engine.task_tools import SKIPPED, tool_result
from va_llm.models import ToolCall, ToolDef

__all__ = [
    "INVALID_ARGUMENTS",
    "MAX_SCHEDULING_ROUNDS",
    "UNKNOWN_TOOL",
    "SchedulingRound",
    "ToolAnswer",
    "run_scheduling_calls",
    "scheduling_tools",
]

MAX_SCHEDULING_ROUNDS: Final = 3
INVALID_ARGUMENTS: Final = tool_result({"error": "invalid_arguments"})
UNKNOWN_TOOL: Final = tool_result({"error": "unknown_tool"})


def scheduling_tools() -> tuple[ToolDef, ...]:
    """:data:`SCHEDULING_FUNCTIONS` as tool definitions."""
    tools: list[ToolDef] = []
    for entry in SCHEDULING_FUNCTIONS:
        function = cast("dict[str, Any]", entry["function"])
        tools.append(
            ToolDef(
                name=function["name"],
                description=function["description"],
                parameters=function["parameters"],
            )
        )
    return tuple(tools)


@dataclass(frozen=True, slots=True)
class ToolAnswer:
    """The tool message answering one call (``content`` is compact JSON)."""

    call_id: str
    name: str
    content: str


@dataclass(frozen=True, slots=True)
class SchedulingRound:
    """The answers of one round, where a failed booking routes, and the booking request reports."""

    answers: tuple[ToolAnswer, ...] = ()
    route_to: str | None = None
    reports: tuple[NodeReport, ...] = ()


def _start_of(call: ToolCall) -> str | None:
    try:
        arguments = orjson.loads(call.arguments_json or "{}")
    except orjson.JSONDecodeError:
        return None
    start = arguments.get("start") if isinstance(arguments, dict) else None
    return start if isinstance(start, str) and start.strip() else None


async def run_scheduling_calls(ctx: TurnContext, calls: Sequence[ToolCall]) -> SchedulingRound:
    """Answer ``calls`` at the context's Scheduling node."""
    answers: list[ToolAnswer] = []
    reports: list[NodeReport] = []
    route_to: str | None = None
    for call in calls:
        if route_to is not None:
            answers.append(ToolAnswer(call.id, call.name, SKIPPED))
            continue
        if call.name == "list_slots":
            answers.append(ToolAnswer(call.id, call.name, tool_result(list_slots(ctx))))
            continue
        if call.name != "book_slot":
            answers.append(ToolAnswer(call.id, call.name, UNKNOWN_TOOL))
            continue
        start = _start_of(call)
        if start is None:
            answers.append(ToolAnswer(call.id, call.name, INVALID_ARGUMENTS))
            continue
        outcome = await book_slot(ctx, start)
        answers.append(ToolAnswer(call.id, call.name, tool_result(outcome.tool_message)))
        if outcome.report is not None:
            reports.append(outcome.report)
        route_to = outcome.route_to
    return SchedulingRound(tuple(answers), route_to, tuple(reports))
