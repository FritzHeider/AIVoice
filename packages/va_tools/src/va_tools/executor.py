"""The in-call tool executor of task-mode calls (specs/13 §5; Decision P08-T02b1).

:class:`CallToolExecutor` is the :class:`va_engine.tool_executor.ToolExecutor` of a call: it offers the call's
custom tools to the model and runs their calls through :class:`~va_tools.runner.ToolRunner`. Every run
reports ``call.tool`` (also invalid arguments, which nothing was sent for); a failure (no 2xx answer) also
reports ``call.error`` ``TOOL_FAILED``. :func:`merged_tools` combines the stored tools of ``tool_ids`` with
the call's inline tools: an inline tool replaces a stored tool of the same name, reserved system tool names
and later duplicates are dropped.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from pydantic import JsonValue

from va_common.logging import get_logger
from va_engine.system_tools import is_reserved
from va_engine.templating import TemplateScope
from va_engine.tool_executor import ToolAnswer
from va_llm.models import ToolCall, ToolDef
from va_tools.models import ToolSpec, function_def
from va_tools.runner import ToolRunner, failure_detail

__all__ = ["CallToolExecutor", "ToolEvents", "merged_tools"]

_log = get_logger(__name__)


class ToolEvents(Protocol):
    """Where a call's tool runs are reported (sync: the media recorder queues its publishes)."""

    def tool(self, data: dict[str, JsonValue]) -> None:
        """One run: the ``call.tool`` event data."""
        ...

    def tool_failed(self, detail: str) -> None:
        """A failed run: the ``call.error`` ``TOOL_FAILED`` detail."""
        ...


def merged_tools(stored: Sequence[ToolSpec], inline: Sequence[ToolSpec]) -> tuple[ToolSpec, ...]:
    """The call's tools: stored ones (in order) not shadowed by an inline tool, then the inline ones."""
    inline_names = {spec.name for spec in inline}
    candidates = [*(spec for spec in stored if spec.name not in inline_names), *inline]
    kept: dict[str, ToolSpec] = {}
    for spec in candidates:
        reason = "reserved" if is_reserved(spec.name) else ("duplicate" if spec.name in kept else None)
        if reason is not None:
            _log.info("tools.tool_skipped", tool=spec.name, reason=reason, inline=spec.inline)
            continue
        kept[spec.name] = spec
    return tuple(kept.values())


class CallToolExecutor:
    """Runs the custom tools of one call (see the module docstring)."""

    def __init__(
        self, specs: Sequence[ToolSpec], *, runner: ToolRunner, events: ToolEvents | None = None
    ) -> None:
        self._specs: Mapping[str, ToolSpec] = {spec.name: spec for spec in specs}
        self._runner = runner
        self._events = events

    def definitions(self) -> tuple[ToolDef, ...]:
        return tuple(function_def(spec) for spec in self._specs.values())

    @property
    def tool_speech(self) -> str | None:
        for spec in self._specs.values():
            if spec.speech and spec.speech.strip():
                return spec.speech
        return None

    def handles(self, name: str) -> bool:
        return name in self._specs

    def speech(self, name: str) -> str | None:
        spec = self._specs.get(name)
        if spec is None or not spec.speech or not spec.speech.strip():
            return None
        return spec.speech

    async def execute(self, call: ToolCall, scope: TemplateScope) -> ToolAnswer:
        spec = self._specs[call.name]
        invocation = await self._runner.run(spec, call.arguments_json, scope)
        if self._events is not None:
            self._events.tool(invocation.event_data())
            if invocation.failed:
                self._events.tool_failed(failure_detail(invocation))
        _log.info(
            "tools.call_ran",
            tool=spec.name,
            outcome=invocation.outcome,
            status=invocation.status_code,
            attempts=invocation.attempts,
            duration_ms=invocation.duration_ms,
        )
        return ToolAnswer(invocation.content, invocation.variables)
