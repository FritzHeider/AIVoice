"""Decision-call test helpers: a scripted structured port, conversation builders, and failure values.

:class:`ScriptedPort` answers ``call_structured`` from a script (raw JSON objects validated against the real
schema exactly like ``va_llm`` does, or exceptions to raise), advances its ``FrozenClock`` by ``cost_s`` per
call, and records every request.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import orjson
from pydantic import BaseModel

from va_common.time import FrozenClock
from va_engine.decisions import DecisionInputs
from va_engine.models.state import TurnRecord
from va_llm.errors import LlmUnavailableError, StructuredOutputError
from va_llm.models import ChatMessage
from va_llm.policy import CallType
from va_llm.structured import tool_schema, validate_arguments

__all__ = [
    "CALL_ID",
    "NOW",
    "ORG_ID",
    "Request",
    "ScriptedPort",
    "history",
    "inputs",
    "invalid",
    "unavailable",
]

NOW: Final = datetime(2026, 11, 3, 14, 5, tzinfo=UTC)
ORG_ID: Final = uuid.UUID("018f0000-0000-7000-8000-000000000001")
CALL_ID: Final = uuid.UUID("018f0000-0000-7000-8000-000000000002")


@dataclass(frozen=True, slots=True)
class Request:
    call_type: CallType
    schema: type[BaseModel]
    org_id: uuid.UUID
    messages: tuple[ChatMessage, ...]
    function_name: str
    description: str
    call_id: uuid.UUID | None

    @property
    def system(self) -> str:
        return self.messages[0].content or ""

    @property
    def user(self) -> str:
        return self.messages[1].content or ""

    def parameters(self) -> dict[str, Any]:
        return tool_schema(self.schema)


@dataclass(slots=True)
class ScriptedPort:
    script: list[Mapping[str, Any] | Exception] = field(default_factory=list)
    clock: FrozenClock = field(default_factory=lambda: FrozenClock(NOW))
    cost_s: float = 0.25
    requests: list[Request] = field(default_factory=list)

    # jscpd:ignore-start  (the fake mirrors the StructuredPort signature)
    async def call_structured[M: BaseModel](  # noqa: PLR0913 - mirrors LlmGateway.call_structured
        self,
        call_type: CallType,
        schema: type[M],
        *,
        org_id: uuid.UUID,
        messages: Sequence[ChatMessage],
        function_name: str,
        description: str,
        call_id: uuid.UUID | None = None,
    ) -> M:
        # jscpd:ignore-end  (signature ends here)
        self.requests.append(
            Request(call_type, schema, org_id, tuple(messages), function_name, description, call_id)
        )
        self.clock.advance(self.cost_s)
        reply = self.script.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return validate_arguments(schema, orjson.dumps(dict(reply)).decode())


def unavailable() -> LlmUnavailableError:
    return LlmUnavailableError("exhausted", provider_id="platform", model="router", last=None)


def invalid(function_name: str) -> StructuredOutputError:
    return StructuredOutputError(
        function_name, ("$: bad",), attempts=2, provider_id="platform", model="router"
    )


def history(*lines: tuple[str, str]) -> tuple[TurnRecord, ...]:
    """Turns from ``(role, content)`` pairs."""
    return tuple(
        TurnRecord(role=role, content=content, node_id=None, at_ms=1000 * index)  # type: ignore[arg-type]
        for index, (role, content) in enumerate(lines)
    )


def inputs(
    latest: str,
    *lines: tuple[str, str],
    variables: Mapping[str, Any] | None = None,
) -> DecisionInputs:
    return DecisionInputs(
        org_id=ORG_ID,
        latest=latest,
        history=history(*lines),
        variables=dict(variables or {}),
        call_id=CALL_ID,
    )
