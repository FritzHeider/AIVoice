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

from va_common.logging import get_logger
from va_llm.metrics import LlmMetrics
from va_llm.models import ChatMessage, TokenUsage, ToolCall
from va_llm.policy import CallType
from va_llm.pricing import CostTable

__all__ = ["Meter", "UsageRecord", "UsageRecorder", "estimate_tokens", "estimate_usage"]

_BYTES_PER_TOKEN: Final = 4
_TOKENS_PER_MESSAGE: Final = 4

_log = get_logger(__name__)


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


class Meter:
    """Adds a completion's tokens to ``va_llm_tokens_total`` and hands its :class:`UsageRecord` to the
    recorder; a recorder failure is logged ``llm.usage_record_failed`` and never raised."""

    def __init__(self, metrics: LlmMetrics, costs: CostTable, recorder: UsageRecorder | None) -> None:
        self._metrics = metrics
        self._costs = costs
        self._recorder = recorder

    async def record(  # noqa: PLR0913 - the fields of one usage record
        self,
        *,
        org_id: uuid.UUID,
        call_id: uuid.UUID | None,
        call_type: CallType,
        provider_id: str,
        model: str,
        usage: TokenUsage,
    ) -> None:
        self._metrics.tokens(model, usage.prompt_tokens, usage.completion_tokens)
        if self._recorder is None:
            return
        cost_in, cost_out = self._costs.cost_micros(model, usage.prompt_tokens, usage.completion_tokens)
        record = UsageRecord(
            org_id=org_id,
            call_id=call_id,
            call_type=call_type,
            provider_id=provider_id,
            model=model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            estimated=usage.estimated,
            cost_in_micros=cost_in,
            cost_out_micros=cost_out,
        )
        try:
            await self._recorder.record(record)
        except Exception as exc:  # metering must never fail a completion
            _log.warning("llm.usage_record_failed", model=model, error=type(exc).__name__)
