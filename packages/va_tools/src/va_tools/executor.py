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

A system tool that ``detaches`` (:attr:`SystemTool.detaches`) has a side effect the caller asked for, so its
body runs to completion even when a barge-in cancels the turn waiting for it (Decision P08-FIX-13 under
specs/13 §6): each run is a task the executor owns, awaited through :func:`asyncio.shield`, and its outcome
(failure event included) is still reported after nobody waits for it any more. The executor tells the round so
(:meth:`CallToolExecutor.detached`), which answers the call ``in_progress`` instead of ``interrupted``
(Decision P08-FIX-21 under specs/13 §6); the cancellation of the wait itself always propagates.
:meth:`CallToolExecutor.aclose` waits for unfinished runs at call end (up to :data:`SYSTEM_DRAIN_S`) and
cancels what is left.

A system tool that does not detach (a lookup) runs in the waiting task and is cancelled with it: the executor
neither owns nor drains the run, a cut round answers it ``interrupted`` and the model asks again (Decision
P08-FIX-22 under specs/13 §6).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from pydantic import JsonValue

from va_common.logging import get_logger
from va_engine.system_tools import is_reserved
from va_engine.templating import TemplateScope
from va_engine.tool_executor import ToolAnswer
from va_llm.models import ToolCall, ToolDef
from va_tools.models import ToolSpec, function_def
from va_tools.runner import ToolRunner, failure_detail

__all__ = [
    "SYSTEM_DRAIN_S",
    "CallToolExecutor",
    "SystemOutcome",
    "SystemTool",
    "ToolEvents",
    "merged_tools",
]

SYSTEM_DRAIN_S: Final = 20.0
"""How long :meth:`CallToolExecutor.aclose` waits for unfinished system tool runs (longer than a callback's
workflow start deadline plus its database writes)."""

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

    @property
    def detaches(self) -> bool:
        """Whether a run goes on when the turn waiting for it is cut (a side effect the caller asked for:
        ``send_sms``, ``schedule_callback``). A tool without one (a lookup) is cancelled with its wait."""
        ...

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
        self._pending: set[asyncio.Task[SystemOutcome]] = set()
        self._detached: list[ToolCall] = []

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

    def detached(self, call: ToolCall) -> bool:
        """Whether the system tool run of ``call`` went on when the wait for it was cancelled (by object
        identity: equal calls are different runs). A run cancelled by :meth:`aclose`, a system tool that
        does not detach and a custom tool, both cancelled with their wait, are not detached."""
        return any(item is call for item in self._detached)

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
        if not tool.detaches:  # nothing to finish for nobody: the run ends with the wait for it
            outcome = await self._finish_system(tool, call)
            return ToolAnswer(outcome.content)
        task = asyncio.create_task(self._finish_system(tool, call), name=f"system-tool:{tool.name}")
        self._pending.add(task)
        task.add_done_callback(self._settled)
        try:
            outcome = await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():  # the wait was cancelled, the run was not: it goes on (or just ended)
                self._detached.append(call)
            raise
        return ToolAnswer(outcome.content)

    async def _finish_system(self, tool: SystemTool, call: ToolCall) -> SystemOutcome:
        """One system tool run, reported here so a run nobody waits for any more is still reported."""
        try:
            outcome = await tool.run(call)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.exception("tools.system_tool_crashed", tool=tool.name, error_type=type(exc).__name__)
            if self._events is not None:
                self._events.tool_failed(f"{tool.name}: crashed")
            raise
        if outcome.failure is not None and self._events is not None:
            self._events.tool_failed(f"{tool.name}: {outcome.failure}")
        _log.info("tools.system_tool_ran", tool=tool.name, failure=outcome.failure)
        return outcome

    def _settled(self, task: asyncio.Task[SystemOutcome]) -> None:
        """Forget a finished run; retrieve its exception (already logged and reported) so a run nobody
        awaits any more never triggers asyncio's "exception was never retrieved"."""
        self._pending.discard(task)
        if not task.cancelled():
            task.exception()

    async def aclose(self, timeout_s: float = SYSTEM_DRAIN_S) -> None:
        """Wait up to ``timeout_s`` for unfinished system tool runs, then cancel the rest (logged
        ``tools.system_tools_abandoned``); never raises."""
        pending = set(self._pending)
        if not pending:
            return
        _done, left = await asyncio.wait(pending, timeout=timeout_s)
        if not left:
            return
        for task in left:
            task.cancel()
        await asyncio.wait(left)
        _log.warning("tools.system_tools_abandoned", count=len(left))
