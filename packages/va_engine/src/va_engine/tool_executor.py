"""The seam through which runners execute tools that need I/O (specs/13 §5, Decision P05-T09c).

Custom tools and the executor-backed system tools (``send_sms``, ``schedule_callback``, ``lookup_knowledge``)
run outside the engine (the phase 08 ToolRunner). Runners call :meth:`ToolExecutor.execute` only with calls
whose name :meth:`ToolExecutor.handles` (system tool arguments are validated first); the returned string is
the content of the ``tool`` message answering the call.
"""

from __future__ import annotations

from typing import Protocol

from va_llm.models import ToolCall, ToolDef

__all__ = ["ToolExecutor"]


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

    async def execute(self, call: ToolCall) -> str:
        """Run ``call`` and return the ``tool`` message content (a failure is reported in the content)."""
        ...
