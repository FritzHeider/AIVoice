"""Tool round planning for the task-mode runner (specs/10 §7, specs/13 §5; Decision P05-T09c).

:func:`plan_round` classifies the tool calls of one model reply in call order: calls past the per-round cap
(or the call-wide budget) are answered ``too_many_tool_calls``, calls after the first terminal call
(``end_call``, ``transfer_call``) are answered ``skipped``, invalid calls are answered with the
``invalid_arguments`` message, and the rest are executed by the runner. :func:`completed_rounds` picks the
tool rounds a superseded turn hands on to the turn that replaces it (specs/13 §4 Decision P08-FIX-17). Pure:
no I/O.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

import orjson

from va_engine.system_tools import (
    UNKNOWN_TOOL,
    EndCallArgs,
    InvalidToolCall,
    PressButtonArgs,
    SystemToolFacts,
    TransferCallArgs,
    parse_system_call,
    resolve_transfer,
)
from va_llm.models import ChatMessage, ToolCall

__all__ = [
    "ENDING",
    "EXECUTOR_CONCURRENCY",
    "FILLER_DELAY_S",
    "INTERRUPTED",
    "MAX_INVALID_ROUNDS",
    "MAX_TOOL_CALLS_PER_CALL",
    "MAX_TOOL_CALLS_PER_ROUND",
    "MAX_TOOL_ROUNDS",
    "SKIPPED",
    "TOOL_FAILED",
    "TOO_MANY_TOOL_CALLS",
    "TRANSFERRING",
    "PlannedCall",
    "RoundPlan",
    "completed_rounds",
    "plan_round",
    "sent_digits",
    "tool_result",
]

MAX_TOOL_ROUNDS: Final = 3
MAX_INVALID_ROUNDS: Final = 2
MAX_TOOL_CALLS_PER_ROUND: Final = 4
MAX_TOOL_CALLS_PER_CALL: Final = 100
EXECUTOR_CONCURRENCY: Final = 3
FILLER_DELAY_S: Final = 1.2
"""A round without tool ``speech`` plays a filler when its tools still run after this long (specs/13 §5)."""

type CallKind = Literal["execute", "press", "end", "transfer", "answer"]


def tool_result(payload: Mapping[str, object]) -> str:
    """Compact JSON content of a ``tool`` message."""
    return orjson.dumps(payload).decode()


ENDING: Final = tool_result({"status": "ending"})
TRANSFERRING: Final = tool_result({"status": "transferring"})
SKIPPED: Final = tool_result({"error": "skipped"})
TOO_MANY_TOOL_CALLS: Final = tool_result({"error": "too_many_tool_calls"})
INTERRUPTED: Final = tool_result({"error": "interrupted"})
TOOL_FAILED: Final = tool_result({"error": "tool_failed"})
_UNKNOWN_LABEL: Final = [{"path": "label", "message": "unknown transfer label"}]


def sent_digits(digits: str) -> str:
    """The ``press_button`` result."""
    return tool_result({"status": "sent", "digits": digits})


@dataclass(frozen=True, slots=True)
class PlannedCall:
    """One call and what the runner does with it; ``answer`` is the precomputed ``tool`` content."""

    call: ToolCall
    kind: CallKind
    answer: str | None = None
    end: EndCallArgs | None = None
    transfer: TransferCallArgs | None = None
    target: str | None = None
    digits: str | None = None


@dataclass(frozen=True, slots=True)
class RoundPlan:
    """The planned calls in call order; ``used`` counts toward the call-wide budget."""

    calls: tuple[PlannedCall, ...]
    invalid: bool
    used: int

    @property
    def terminal(self) -> PlannedCall | None:
        """The ``end_call``/``transfer_call`` that ends the turn, if any."""
        return next((item for item in self.calls if item.kind in ("end", "transfer")), None)

    @property
    def executed(self) -> tuple[PlannedCall, ...]:
        """The calls the tool executor runs."""
        return tuple(item for item in self.calls if item.kind == "execute")


def _invalid(call: ToolCall, invalid: InvalidToolCall) -> PlannedCall:
    return PlannedCall(call, "answer", answer=invalid.tool_message(call.id).content)


def _transfer(call: ToolCall, args: TransferCallArgs, facts: SystemToolFacts) -> PlannedCall:
    target = resolve_transfer(args, facts)
    if target is None:
        return _invalid(call, InvalidToolCall(call.name, _UNKNOWN_LABEL))
    return PlannedCall(call, "transfer", answer=TRANSFERRING, transfer=args, target=target)


def _classify(call: ToolCall, facts: SystemToolFacts, handles: Callable[[str], bool]) -> PlannedCall:
    parsed = parse_system_call(call, facts)
    match parsed:
        case InvalidToolCall():
            return _invalid(call, parsed)
        case EndCallArgs():
            return PlannedCall(call, "end", answer=ENDING, end=parsed)
        case TransferCallArgs():
            return _transfer(call, parsed, facts)
        case PressButtonArgs():
            return PlannedCall(call, "press", answer=sent_digits(parsed.digits), digits=parsed.digits)
        case _ if handles(call.name):
            return PlannedCall(call, "execute")
        case _:
            return _invalid(call, InvalidToolCall(call.name, UNKNOWN_TOOL))


def plan_round(
    calls: Sequence[ToolCall], facts: SystemToolFacts, *, handles: Callable[[str], bool], budget: int
) -> RoundPlan:
    """Classify ``calls``; ``handles`` says which names the tool executor runs, ``budget`` is how many tool
    calls the call may still make."""
    limit = max(0, min(MAX_TOOL_CALLS_PER_ROUND, budget))
    planned: list[PlannedCall] = []
    terminal = False
    invalid = False
    used = 0
    for call in calls:
        if terminal:
            planned.append(PlannedCall(call, "answer", answer=SKIPPED))
            continue
        if used >= limit:
            planned.append(PlannedCall(call, "answer", answer=TOO_MANY_TOOL_CALLS))
            continue
        item = _classify(call, facts, handles)
        used += 1
        terminal = item.kind in ("end", "transfer")
        invalid = invalid or (item.kind == "answer")
        planned.append(item)
    return RoundPlan(tuple(planned), invalid=invalid, used=used)


def completed_rounds(messages: Sequence[ChatMessage]) -> list[ChatMessage]:
    """The completed tool rounds in ``messages``.

    Each assistant message with tool calls (without its text: nobody heard it) followed by its ``tool``
    answers; a round lacking an answer for every call, and every other message, is left out.
    """
    rounds: list[ChatMessage] = []
    position = 0
    while position < len(messages):
        message = messages[position]
        position += 1
        if message.role != "assistant" or not message.tool_calls:
            continue
        answers: list[ChatMessage] = []
        while position < len(messages) and messages[position].role == "tool":
            answers.append(messages[position])
            position += 1
        if len(answers) == len(message.tool_calls):
            rounds.extend((message.model_copy(update={"content": None}), *answers))
    return rounds
