"""The seam through which runners execute tools that need I/O (specs/13 §5, Decisions P05-T09c, P08-T02b1).

Custom tools and the executor-backed system tools (``send_sms``, ``schedule_callback``, ``lookup_knowledge``)
run outside the engine (``va_tools.executor.CallToolExecutor``). Runners call :meth:`ToolExecutor.execute`
only with calls whose name :meth:`ToolExecutor.handles` (system tool arguments are validated first); the
returned :class:`ToolAnswer` carries the content of the ``tool`` message answering the call and the variables
the call extracted. A run the executor keeps going after the wait for its answer was cancelled is reported
by :meth:`ToolExecutor.detached` (specs/13 §6, Decision P08-FIX-21). Pathway calls use a
:class:`PathwayToolExecutor`, which hands every node the :class:`ToolExecutor` of exactly the tools that
node's refs name (specs/13 §4, Decision P08-FIX-17).
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

    def detached(self, call: ToolCall) -> bool:
        """Whether a run of ``call`` (this very object: call ids may be empty or repeated) goes on although
        the wait for its answer was cancelled. A cut round answers such a call ``in_progress`` instead of
        ``interrupted`` (specs/13 §6, Decision P08-FIX-21)."""
        ...


class PathwayToolExecutor(Protocol):
    """The tools of a pathway call, resolved per node (specs/13 §4, Decision P08-FIX-17)."""

    @property
    def tool_speech(self) -> str | None:
        """The call's tool speech (the tool usage note quotes it)."""
        ...

    def handles(self, name: str) -> bool:
        """Whether the call runs the executor-backed system tool ``name``."""
        ...

    def for_refs(self, refs: Sequence[ToolRef]) -> ToolExecutor:
        """The executor of one node.

        Its ``definitions()`` are the tools ``refs`` name, in ref order: a string is that stored tool, a
        mapping is that inline tool; refs that resolve to nothing are skipped, and of several refs with one
        tool name the first wins. ``handles``/``speech``/``execute`` answer for exactly those definitions
        plus the call's system tools, so the definition a node shows the model is the one that runs there.
        """
        ...
