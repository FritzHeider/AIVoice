"""Composed final lines for End Call / Transfer Call nodes (specs/11 §3.2.2-§3.2.3, Decision P07-T06 rule 3).

:class:`GatewayLinePort` implements :class:`~va_engine.handlers.LinePort` over the LLM gateway's non-streaming
``dialogue`` completion: the system message is the pathway's global prompt (rendered) followed by the line
instruction, then the last :data:`HISTORY_TURNS` caller/agent messages. An unavailable LLM or a blank answer
raises :class:`~va_engine.handlers.LineUnavailableError`, so the handler falls back to the static ``speech``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Final, Protocol

from va_engine.handlers.base import LineUnavailableError, TurnContext
from va_llm.errors import LlmUnavailableError
from va_llm.models import ChatMessage, Completion
from va_llm.policy import CallType

__all__ = ["HISTORY_TURNS", "LINE_MAX_TOKENS", "CompletionPort", "GatewayLinePort", "line_messages"]

HISTORY_TURNS: Final = 10
LINE_MAX_TOKENS: Final = 120
_LINE_HEADER: Final = (
    "## Your next line\nWrite only the line you say now: one or two short sentences, no markdown. "
    "Instruction: {instruction}"
)
_CONVERSATION_ROLES: Final = frozenset({"user", "assistant"})


class CompletionPort(Protocol):
    """Where completions come from (:meth:`va_llm.LlmGateway.complete`)."""

    async def complete(  # noqa: PLR0913 - mirrors LlmGateway.complete
        self,
        call_type: CallType,
        *,
        org_id: uuid.UUID,
        messages: Sequence[ChatMessage],
        temperature: float = 0.0,
        max_tokens: int = 512,
        model: str | None = None,
        call_id: uuid.UUID | None = None,
    ) -> Completion: ...


def line_messages(ctx: TurnContext, instruction: str) -> list[ChatMessage]:
    """The request messages for one composed line."""
    parts: list[str] = []
    global_prompt = ctx.graph.global_config.global_prompt
    if global_prompt and global_prompt.strip():
        rendered = ctx.render(global_prompt, field_name="globalPrompt").strip()
        if rendered:
            parts.append(rendered)
    parts.append(_LINE_HEADER.format(instruction=instruction))
    turns = [turn for turn in ctx.state.history if turn.role in _CONVERSATION_ROLES][-HISTORY_TURNS:]
    return [
        ChatMessage(role="system", content="\n\n".join(parts)),
        *(ChatMessage(role=turn.role, content=turn.content) for turn in turns),
    ]


class GatewayLinePort:
    """Composes final lines through the LLM gateway for one conversation."""

    def __init__(
        self,
        gateway: CompletionPort,
        *,
        org_id: uuid.UUID,
        call_id: uuid.UUID | None = None,
        model: str | None = None,
    ) -> None:
        self._gateway = gateway
        self._org_id = org_id
        self._call_id = call_id
        self._model = model

    async def compose(self, ctx: TurnContext, instruction: str) -> str:
        """One line following ``instruction``; :class:`LineUnavailableError` when none comes back."""
        options = ctx.node.data.model_options
        global_config = ctx.graph.global_config
        temperature = options.temperature
        if temperature is None:
            temperature = global_config.default_temperature
        try:
            completion = await self._gateway.complete(
                "dialogue",
                org_id=self._org_id,
                messages=line_messages(ctx, instruction),
                temperature=float(temperature),
                max_tokens=LINE_MAX_TOKENS,
                model=options.model or global_config.dialogue_model or self._model,
                call_id=self._call_id,
            )
        except LlmUnavailableError as exc:
            raise LineUnavailableError("llm_unavailable") from exc
        line = (completion.content or "").strip()
        if not line:
            raise LineUnavailableError("blank")
        return line
