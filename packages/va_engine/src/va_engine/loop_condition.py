"""Loop-condition scoring (specs/11 §5.4; specs/10 §9.2; specs/11 §5.5 Decision P07-T04 rules 4 and 6).

:meth:`LoopCondition.score` asks the router model (``score_condition``) how likely it is that a node's
``condition`` has been satisfied; the node's ``conditionThreshold`` decides. A failed call counts as "not met"
so the conversation keeps working on the step. The result is pure data; the turn orchestration emits
``call.loop_condition`` and adds :func:`not_met_suffix` to the dialogue prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from pydantic import JsonValue

from va_common.logging import get_logger
from va_common.time import Clock
from va_engine.decision_schemas import ConditionScore
from va_engine.decisions import (
    DecisionInputs,
    StructuredPort,
    elapsed_ms,
    latest_line,
    one_line,
    transcript_lines,
)
from va_engine.models.nodes import ConditionExample, PathwayNode
from va_llm.errors import LlmUnavailableError, StructuredOutputError
from va_llm.models import ChatMessage
from va_llm.prompts import render_prompt

__all__ = [
    "CONDITION_HISTORY",
    "UNAVAILABLE_MESSAGE",
    "ConditionResult",
    "LoopCondition",
    "not_met_suffix",
    "render_condition_prompt",
]

CONDITION_HISTORY: Final = 10
UNAVAILABLE_MESSAGE: Final = "Condition check unavailable"
CONDITION_DESCRIPTION: Final = "Score how likely it is that the completion condition has been satisfied."
_ROLE_PREFIX: Final = {"user": "Caller", "assistant": "Agent"}

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ConditionResult:
    """A scored loop condition. ``score`` is ``None`` (and ``met`` False) when the check failed;
    ``model_met`` is the model's own verdict, kept for diagnostics only."""

    condition: str
    threshold: int
    score: int | None
    met: bool
    model_met: bool | None = None
    missing: str = ""
    degraded: bool = False
    ms: int = 0

    @property
    def message(self) -> str:
        """``Likely {score}/10`` (the ``call.loop_condition`` message)."""
        return UNAVAILABLE_MESSAGE if self.score is None else f"Likely {self.score}/10"

    def event_data(self) -> dict[str, JsonValue]:
        """``call.loop_condition`` data (specs/19 §2)."""
        return {"score": self.score, "met": self.met}


def not_met_suffix(condition: str) -> str:
    """The dialogue prompt suffix while the condition is not met (specs/11 §5.4)."""
    return f'The following condition is not yet satisfied; keep working toward it: "{condition}"'


def _example_line(example: ConditionExample) -> str:
    lines = [
        f"{_ROLE_PREFIX[message.role]}: {one_line(message.content)}"
        for message in example.conversation
        if message.role in _ROLE_PREFIX
    ]
    verdict = "true" if example.condition_met else "false"
    return f"- Conversation: {' / '.join(lines)} → satisfied: {verdict} ({one_line(example.explanation)})"


def render_condition_prompt(node: PathwayNode) -> str:
    """The specs/10 §9.2 system prompt for ``node``'s condition (examples from ``conditionExamples``)."""
    examples = [_example_line(example) for example in node.data.condition_examples]
    return render_prompt(
        "loop_condition",
        {
            "condition": one_line(node.data.condition or ""),
            "examples_block": "Examples:\n" + "\n".join(examples) + "\n\n" if examples else "",
        },
    )


class LoopCondition:
    """Scores node loop conditions over the router model."""

    def __init__(self, gateway: StructuredPort, *, clock: Clock) -> None:
        self._gateway = gateway
        self._clock = clock

    async def score(self, node: PathwayNode, inputs: DecisionInputs) -> ConditionResult | None:
        """The scored condition of ``node`` (``None`` when it has none); never raises for LLM failures."""
        condition = (node.data.condition or "").strip()
        if not condition:
            return None
        threshold = node.data.condition_threshold
        lines = transcript_lines(inputs.history, CONDITION_HISTORY)
        lines.append(latest_line(inputs.latest))
        messages = [
            ChatMessage(role="system", content=render_condition_prompt(node)),
            ChatMessage(role="user", content="\n".join(lines)),
        ]
        started = self._clock.monotonic()
        try:
            output = await self._gateway.call_structured(
                "loop_condition",
                ConditionScore,
                org_id=inputs.org_id,
                messages=messages,
                function_name="score_condition",
                description=CONDITION_DESCRIPTION,
                call_id=inputs.call_id,
            )
        except (LlmUnavailableError, StructuredOutputError) as error:
            _logger.warning("engine.loop_condition_degraded", node_id=node.id, reason=type(error).__name__)
            ms = elapsed_ms(started, self._clock.monotonic())
            return ConditionResult(condition, threshold, None, met=False, degraded=True, ms=ms)
        return ConditionResult(
            condition,
            threshold,
            output.score,
            met=output.score >= threshold,
            model_met=output.met,
            missing=output.missing,
            ms=elapsed_ms(started, self._clock.monotonic()),
        )
