"""Gateway request and result types (specs/10-llm-gateway.md §1-§2; Decision 2026-11-01 P03-T13a rule 11).

Messages and tools are sent in the OpenAI chat-completions wire shape; :class:`Completion` is what
:meth:`va_llm.LlmGateway.complete` returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ChatMessage", "Completion", "ForceTool", "TokenUsage", "ToolCall", "ToolChoice", "ToolDef"]

type Role = Literal["system", "user", "assistant", "tool"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ToolCall(_Frozen):
    """A function call the model asked for; ``arguments_json`` is the raw JSON text the model produced."""

    id: str
    name: str
    arguments_json: str

    def wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments_json},
        }


class ChatMessage(_Frozen):
    """One chat message; assistant messages may carry tool calls, tool messages answer one by id."""

    role: Role
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    def wire(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [call.wire() for call in self.tool_calls]
        if self.tool_call_id is not None:
            message["tool_call_id"] = self.tool_call_id
        return message


class ToolDef(_Frozen):
    """A function the model may call: name, description, and a JSON Schema object for its parameters."""

    name: str = Field(min_length=1)
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})

    def wire(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


@dataclass(frozen=True, slots=True)
class ForceTool:
    """``tool_choice`` forcing one function by name."""

    name: str

    def wire(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name}}


type ToolChoice = Literal["auto", "none", "required"] | ForceTool


class TokenUsage(_Frozen):
    """Prompt and completion tokens; ``estimated`` when the provider sent no ``usage`` (specs/10 §11)."""

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    estimated: bool = False


@dataclass(frozen=True, slots=True)
class Completion:
    """A finished non-streaming completion and how it was obtained.

    ``attempts`` counts every request sent (retries and the fallback included); ``fell_back`` is true when the
    answer came from the fallback target.
    """

    content: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str | None
    usage: TokenUsage
    provider_id: str
    model: str
    attempts: int
    fell_back: bool
