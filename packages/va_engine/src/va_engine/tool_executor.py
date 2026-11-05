"""The seam through which runners execute tools that need I/O (specs/13 §5, Decisions P05-T09c, P08-T02b1).

Custom tools and the executor-backed system tools (``send_sms``, ``schedule_callback``, ``lookup_knowledge``)
run outside the engine (``va_tools.executor.CallToolExecutor``). Runners call :meth:`ToolExecutor.execute`
only with calls whose name :meth:`ToolExecutor.handles` (system tool arguments are validated first); the
returned :class:`ToolAnswer` carries the content of the ``tool`` message answering the call and the variables
the call extracted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import JsonValue

from va_engine.templating import TemplateScope
from va_llm.models import ToolCall, ToolDef

__all__ = ["ToolAnswer", "ToolExecutor"]


@dataclass(frozen=True, slots=True)
class ToolAnswer:
    """What one executed tool call answers: the ``tool`` message and the variables to merge."""

    content: str
    variables: Mapping[str, JsonValue] = field(default_factory=dict)


class ToolExecutor(Protocol):
    def definitions(self) -> tuple[ToolDef, ...]:
        """The custom tools offered to the model (system tools are added by the runner)."""
        ...

    @property
    def tool_speech(self) -> str | None:
        """The first custom tool's speech (the tool usage note quotes it)."""
        ...

    def handles(self, name: str) -> bool:
        """Whether the executor runs tool ``name``."""
        ...

    def speech(self, name: str) -> str | None:
        """The ``speech`` template of tool ``name`` (``None`` when it has none)."""
        ...

    async def execute(self, call: ToolCall, scope: TemplateScope) -> ToolAnswer:
        """Run ``call`` with templates rendered in ``scope``; a failure is reported in the answer content."""
        ...
