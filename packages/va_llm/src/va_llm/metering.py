"""Token usage and usage records (specs/10-llm-gateway.md §11; Decision P03-T13a rule 9).

When a provider sends no ``usage`` the gateway estimates it offline: ``ceil(utf8_bytes / 4)`` per text plus 4
per prompt message, flagged ``estimated`` (tiktoken arrives with the conversation window, DEC-006).
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from va_llm.models import ChatMessage, TokenUsage, ToolCall
from va_llm.policy import CallType

__all__ = ["UsageRecord", "UsageRecorder", "estimate_tokens", "estimate_usage"]

_BYTES_PER_TOKEN: Final = 4
_TOKENS_PER_MESSAGE: Final = 4


def estimate_tokens(text: str) -> int:
    """The offline token estimate of one text."""
    return math.ceil(len(text.encode()) / _BYTES_PER_TOKEN)


def _calls_tokens(calls: Iterable[ToolCall]) -> int:
    return sum(estimate_tokens(call.name) + estimate_tokens(call.arguments_json) for call in calls)


def estimate_usage(
    messages: Sequence[ChatMessage], content: str | None, tool_calls: Sequence[ToolCall]
) -> TokenUsage:
    """Estimated usage of a completion of ``messages`` that produced ``content`` and ``tool_calls``."""
    prompt = sum(
        _TOKENS_PER_MESSAGE + estimate_tokens(message.content or "") + _calls_tokens(message.tool_calls)
        for message in messages
    )
    completion = estimate_tokens(content or "") + _calls_tokens(tool_calls)
    return TokenUsage(prompt_tokens=prompt, completion_tokens=completion, estimated=True)


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """One completion's tokens and cost (micro-dollars), for the ``llm_tokens_in``/``llm_tokens_out`` usage
    events of specs/10 §11."""

    org_id: uuid.UUID
    call_id: uuid.UUID | None
    call_type: CallType
    provider_id: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    estimated: bool
    cost_in_micros: int
    cost_out_micros: int


class UsageRecorder(Protocol):
    """Persists usage records (the billing phase writes ``usage_events``)."""

    async def record(self, record: UsageRecord) -> None: ...
