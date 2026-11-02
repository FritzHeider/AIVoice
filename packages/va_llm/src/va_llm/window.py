"""Conversation window trimming (specs/10-llm-gateway.md §6; Decision 2026-11-02 P05-T09b).

The history is split into turns: a new turn starts at every ``user`` message, so an assistant reply, its tool
calls, their ``tool`` results, and context updates leave together. The opener (the first message, when it is
an assistant message without tool calls) and the current turn are never dropped; when anything was dropped the
:data:`OMITTED_NOTE` system message marks the cut.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from va_llm.models import ChatMessage
from va_llm.tokens import TokenCounter

__all__ = ["OMITTED_NOTE", "ConversationWindow", "WindowFit", "split_turns"]

OMITTED_NOTE = "(Earlier parts of the conversation are omitted.)"
_NOTE = ChatMessage(role="system", content=OMITTED_NOTE)
_INVALID_BUDGET = "budget_tokens must be positive"


@dataclass(frozen=True, slots=True)
class WindowFit:
    """The trimmed window: ``dropped`` turns were removed; ``over_budget`` when the minimum does not fit."""

    messages: tuple[ChatMessage, ...]
    dropped: int
    tokens: int
    over_budget: bool


def split_turns(
    history: Sequence[ChatMessage],
) -> tuple[ChatMessage | None, list[tuple[ChatMessage, ...]]]:
    """``(opener, turns)``: the opener (or ``None``) and the rest grouped at ``user`` messages."""
    opener: ChatMessage | None = None
    rest = list(history)
    if rest and rest[0].role == "assistant" and not rest[0].tool_calls:
        opener = rest.pop(0)
    turns: list[tuple[ChatMessage, ...]] = []
    current: list[ChatMessage] = []
    for message in rest:
        if message.role == "user" and current:
            turns.append(tuple(current))
            current = []
        current.append(message)
    if current:
        turns.append(tuple(current))
    return opener, turns


class ConversationWindow:
    """Fits a history into ``budget_tokens`` (the org setting ``llm.context_budget_tokens``)."""

    def __init__(self, budget_tokens: int, counter: TokenCounter) -> None:
        if budget_tokens <= 0:
            raise ValueError(_INVALID_BUDGET)
        self._budget = budget_tokens
        self._counter = counter

    @property
    def budget_tokens(self) -> int:
        return self._budget

    def fit(self, history: Sequence[ChatMessage]) -> WindowFit:
        """Drop the oldest whole turns until the window fits; keep the opener and the current turn."""
        opener, turns = split_turns(history)
        head = () if opener is None else (opener,)
        sizes = [self._counter.messages(turn) for turn in turns]
        fixed = self._counter.messages(head)
        total = fixed + sum(sizes)
        if total <= self._budget:
            return WindowFit(messages=tuple(history), dropped=0, tokens=total, over_budget=False)
        note = self._counter.message(_NOTE)
        dropped = 0
        remaining = total + note
        while dropped < len(turns) - 1 and remaining > self._budget:
            remaining -= sizes[dropped]
            dropped += 1
        if dropped == 0:
            return WindowFit(messages=tuple(history), dropped=0, tokens=total, over_budget=True)
        kept = [message for turn in turns[dropped:] for message in turn]
        return WindowFit(
            messages=(*head, _NOTE, *kept),
            dropped=dropped,
            tokens=remaining,
            over_budget=remaining > self._budget,
        )
