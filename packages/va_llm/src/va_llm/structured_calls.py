"""One structured-output call: forced tool call or JSON mode, validation, one repair request
(specs/10-llm-gateway.md §3; Decision 2026-11-01 P03-T14a rules 1, 4-5, 8-10).

Each request is one :meth:`va_llm.LlmGateway.complete` call, so it gets the call type's policy, retries,
breaker skip, fallback, metering, and its ``llm.<call_type>`` span.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

from va_common.logging import get_logger
from va_llm.errors import OrgProviderError, StructuredOutputError
from va_llm.metrics import LlmMetrics
from va_llm.models import ChatMessage, Completion, ForceTool, ToolCall, ToolDef
from va_llm.policy import CallType
from va_llm.structured import (
    InvalidOutputError,
    check_function,
    first_json_object,
    json_mode_instruction,
    repair_message,
    tool_schema,
    validate_arguments,
)

__all__ = ["StructuredCall", "run_structured"]

if TYPE_CHECKING:
    from va_llm.gateway import LlmGateway

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StructuredCall[M: BaseModel]:
    """The arguments of one :meth:`va_llm.LlmGateway.call_structured` call."""

    call_type: CallType
    schema: type[M]
    org_id: uuid.UUID
    messages: tuple[ChatMessage, ...]
    function_name: str
    description: str
    call_id: uuid.UUID | None
    temperature: float
    max_tokens: int

    def check(self) -> None:
        if self.call_type == "dialogue":
            msg = "dialogue does not use structured outputs (it streams)"
            raise ValueError(msg)
        check_function(self.function_name, self.schema)


class _BadOutputError(Exception):
    """An invalid output: its rendered errors, the raw text to echo back, and whether a forced request got no
    tool call (the repair then uses JSON mode)."""

    def __init__(self, errors: Sequence[str], raw: str, *, missing_tool_call: bool) -> None:
        super().__init__("; ".join(errors))
        self.errors = tuple(errors)
        self.raw = raw
        self.missing_tool_call = missing_tool_call


async def run_structured[M: BaseModel](
    gateway: LlmGateway, metrics: LlmMetrics, call: StructuredCall[M]
) -> M:
    """The validated output of ``call``; :class:`StructuredOutputError` after an invalid repair."""
    call.check()
    forced = await _supports_tool_choice(gateway, call.org_id)
    completion = await _request(gateway, call, forced=forced, extra=())
    first = _outcome(call, completion, forced=forced)
    if not isinstance(first, _BadOutputError):
        metrics.structured(call.function_name, "valid")
        return first
    _invalid(call, 1, first)
    repair_forced = forced and not first.missing_tool_call
    extra = (
        ChatMessage(role="assistant", content=first.raw),
        ChatMessage(role="user", content=repair_message(call.function_name, first.errors)),
    )
    completion = await _request(gateway, call, forced=repair_forced, extra=extra)
    second = _outcome(call, completion, forced=repair_forced)
    if not isinstance(second, _BadOutputError):
        metrics.structured(call.function_name, "repaired")
        return second
    _invalid(call, 2, second)
    metrics.structured(call.function_name, "invalid")
    raise StructuredOutputError(
        call.function_name,
        second.errors,
        attempts=2,
        provider_id=completion.provider_id,
        model=completion.model,
    ) from second


def _outcome[M: BaseModel](
    call: StructuredCall[M], completion: Completion, *, forced: bool
) -> M | _BadOutputError:
    try:
        return _parse(call, completion, forced=forced)
    except _BadOutputError as exc:
        return exc


async def _supports_tool_choice(gateway: LlmGateway, org_id: uuid.UUID) -> bool:
    """The provider's flag; the platform provider's when the org's provider cannot be loaded (``complete``
    then applies the §1 rules for that failure)."""
    try:
        provider = await gateway.registry.resolve(org_id)
    except OrgProviderError:
        provider = gateway.registry.platform
    return provider.supports_tool_choice


def _request[M: BaseModel](
    gateway: LlmGateway, call: StructuredCall[M], *, forced: bool, extra: tuple[ChatMessage, ...]
) -> Awaitable[Completion]:
    if forced:
        tool = ToolDef(
            name=call.function_name, description=call.description, parameters=tool_schema(call.schema)
        )
        return gateway.complete(
            call.call_type,
            org_id=call.org_id,
            messages=(*call.messages, *extra),
            tools=(tool,),
            tool_choice=ForceTool(call.function_name),
            temperature=call.temperature,
            max_tokens=call.max_tokens,
            call_id=call.call_id,
        )
    instruction = ChatMessage(role="system", content=json_mode_instruction(call.schema))
    return gateway.complete(
        call.call_type,
        org_id=call.org_id,
        messages=(*call.messages, instruction, *extra),
        temperature=call.temperature,
        max_tokens=call.max_tokens,
        call_id=call.call_id,
    )


def _parse[M: BaseModel](call: StructuredCall[M], completion: Completion, *, forced: bool) -> M:
    if forced:
        tool_call = _pick(completion.tool_calls, call.function_name)
        if tool_call is not None:
            return _validated(call.schema, tool_call.arguments_json, missing_tool_call=False)
    text = first_json_object(completion.content)
    if text is None:
        problem = "no tool call and no JSON object in the reply" if forced else "no JSON object in the reply"
        raise _BadOutputError([f"$: {problem}"], completion.content or "", missing_tool_call=forced)
    return _validated(call.schema, text, missing_tool_call=forced)


def _pick(tool_calls: Sequence[ToolCall], name: str) -> ToolCall | None:
    """The first call of ``name``, else the first call (``None`` when there is none)."""
    return next((item for item in tool_calls if item.name == name), tool_calls[0] if tool_calls else None)


def _validated[M: BaseModel](schema: type[M], text: str, *, missing_tool_call: bool) -> M:
    try:
        return validate_arguments(schema, text)
    except InvalidOutputError as exc:
        raise _BadOutputError(exc.errors, text, missing_tool_call=missing_tool_call) from exc


def _invalid[M: BaseModel](call: StructuredCall[M], attempt: int, error: _BadOutputError) -> None:
    _log.warning(
        "llm.structured_invalid",
        function=call.function_name,
        call_type=call.call_type,
        attempt=attempt,
        errors=len(error.errors),
    )
