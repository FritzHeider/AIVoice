"""The seam through which runners execute tools that need I/O (specs/13 §5, Decisions P05-T09c, P08-T02b1).

Custom tools and the executor-backed system tools (``send_sms``, ``schedule_callback``, ``lookup_knowledge``)
run outside the engine (``va_tools.executor.CallToolExecutor``). Runners call :meth:`ToolExecutor.execute`
only with calls whose name :meth:`ToolExecutor.handles` (system tool arguments are validated first); the
returned :class:`ToolAnswer` carries the content of the ``tool`` message answering the call and the variables
the call extracted. Pathway calls use a :class:`PathwayToolExecutor`, which also resolves node tool refs
(Decision P08-T02c1).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import JsonValue

from va_engine.templating import TemplateScope
from va_llm.models import ToolCall, ToolDef

__all__ = ["PathwayToolExecutor", "ToolAnswer", "ToolExecutor", "ToolRef"]

type ToolRef = str | Mapping[str, JsonValue]
"""A node ``tools`` / ``globalTools`` entry: a stored tool id (``TL-<uuid>`` or a bare uuid) or an inline
tool."""


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


class PathwayToolExecutor(ToolExecutor, Protocol):
    """An executor that also resolves the tools a pathway node names (specs/13 §4, Decision P08-T02c1)."""

    def definitions_for(self, refs: Sequence[ToolRef]) -> tuple[ToolDef, ...]:
        """The executor's tools named by ``refs`` in ref order (a string = a stored tool id, a mapping = an
        inline tool by its ``name``); unknown refs are skipped and a tool named twice is listed once."""
        ...
