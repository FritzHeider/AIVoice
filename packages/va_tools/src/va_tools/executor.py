"""The in-call tool executor of task-mode calls (specs/13 §5; Decision P08-T02b1).

:class:`CallToolExecutor` is the :class:`va_engine.tool_executor.ToolExecutor` of a call: it offers the call's
custom tools to the model and runs their calls through :class:`~va_tools.runner.ToolRunner`. Every run
reports ``call.tool`` (also invalid arguments, which nothing was sent for); a failure (no 2xx answer) also
reports ``call.error`` ``TOOL_FAILED``. :func:`merged_tools` combines the stored tools of ``tool_ids`` with
the call's inline tools: an inline tool replaces a stored tool of the same name, reserved system tool names
and later duplicates are dropped.

The executor also runs the call's executor-backed system tools (:class:`SystemTool`: ``send_sms``,
``schedule_callback``; Decision P08-T02b2 under specs/13 §6). They are not in :meth:`definitions` (the runner
offers system tools from the call's facts), have no ``speech`` (a slow one gets a filler), and report no
``call.tool``: their domain events are their own; a :class:`SystemOutcome` with a ``failure`` also reports
``call.error`` ``TOOL_FAILED`` (``<tool>: <failure>``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import JsonValue

from va_common.logging import get_logger
from va_engine.system_tools import is_reserved
from va_engine.templating import TemplateScope
from va_engine.tool_executor import ToolAnswer
from va_llm.models import ToolCall, ToolDef
from va_tools.models import ToolSpec, function_def
from va_tools.runner import ToolRunner, failure_detail

__all__ = ["CallToolExecutor", "SystemOutcome", "SystemTool", "ToolEvents", "merged_tools"]

_log = get_logger(__name__)


class ToolEvents(Protocol):
    """Where a call's tool runs are reported (sync: the media recorder queues its publishes)."""

    def tool(self, data: dict[str, JsonValue]) -> None:
        """One run: the ``call.tool`` event data."""
        ...

    def tool_failed(self, detail: str) -> None:
        """A failed run: the ``call.error`` ``TOOL_FAILED`` detail."""
        ...

    def sms_sent(self, data: dict[str, JsonValue]) -> None:
        """A message the ``send_sms`` tool sent: the ``call.sms_sent`` event data."""
        ...


@dataclass(frozen=True, slots=True)
class SystemOutcome:
    """What a system tool answers: the ``tool`` message content and, for a failure, its short reason."""

    content: str
    failure: str | None = None


class SystemTool(Protocol):
    """An executor-backed system tool of one call (its arguments were validated by the runner)."""

    @property
    def name(self) -> str: ...

    async def run(self, call: ToolCall) -> SystemOutcome: ...


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
        self,
        specs: Sequence[ToolSpec],
        *,
        runner: ToolRunner,
        events: ToolEvents | None = None,
        system: Sequence[SystemTool] = (),
    ) -> None:
        self._specs: Mapping[str, ToolSpec] = {spec.name: spec for spec in specs}
        self._system: Mapping[str, SystemTool] = {tool.name: tool for tool in system}
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
        return name in self._specs or name in self._system

    def speech(self, name: str) -> str | None:
        spec = self._specs.get(name)
        if spec is None or not spec.speech or not spec.speech.strip():
            return None
        return spec.speech

    async def execute(self, call: ToolCall, scope: TemplateScope) -> ToolAnswer:
        system = self._system.get(call.name)
        if system is not None:
            return await self._run_system(system, call)
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

    async def _run_system(self, tool: SystemTool, call: ToolCall) -> ToolAnswer:
        outcome = await tool.run(call)
        if outcome.failure is not None and self._events is not None:
            self._events.tool_failed(f"{tool.name}: {outcome.failure}")
        _log.info("tools.system_tool_ran", tool=tool.name, failure=outcome.failure)
        return ToolAnswer(outcome.content)
