"""Executor tool rounds shared by the task-mode runner and the pathway engine (specs/13 §5; Decisions
P08-T02b1, P08-T02c1).

The executed calls of one round start as tasks at once (:func:`start_tools`, at most
:data:`~va_engine.task_tools.EXECUTOR_CONCURRENCY` in parallel, all rendered with the variables of the
round's start). :class:`ToolLines` picks what the caller hears while they run: the rendered ``speech`` of the
first executed call whose tool has one, else (when a tool still runs after
:data:`~va_engine.task_tools.FILLER_DELAY_S` on the injected clock) one filler of
:data:`~va_voice.phrase_cache.TOOL_FILLERS`, round-robin per conversation. :func:`merge_variables` merges the
round's extractions in call order.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from typing import Any

import orjson
from pydantic import JsonValue

from va_common.logging import get_logger
from va_common.time import Clock
from va_engine.system_tools import SystemToolFacts
from va_engine.task_tools import EXECUTOR_CONCURRENCY, FILLER_DELAY_S, TOOL_FAILED, PlannedCall
from va_engine.templating import TemplateScope, TemplateSyntaxError, render
from va_engine.tool_executor import ToolAnswer, ToolExecutor
from va_llm.models import ToolCall
from va_voice.phrase_cache import TOOL_FILLERS

__all__ = [
    "ToolLines",
    "call_arguments",
    "execute_gated",
    "merge_variables",
    "runnable_facts",
    "start_tools",
    "stop_tasks",
]

_log = get_logger(__name__)


def call_arguments(call: ToolCall) -> dict[str, object]:
    """The call's arguments as a mapping (``{}`` when they are not a JSON object)."""
    try:
        value: object = orjson.loads(call.arguments_json or "{}")
    except orjson.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def runnable_facts(facts: SystemToolFacts, executor: ToolExecutor | None) -> SystemToolFacts:
    """``facts`` without the executor-backed system tools the executor does not run (never offered)."""

    def runs(name: str) -> bool:
        return executor is not None and executor.handles(name)

    return dataclasses.replace(
        facts,
        sms_capable=facts.sms_capable and runs("send_sms"),
        allow_schedule_callback=facts.allow_schedule_callback and runs("schedule_callback"),
        has_knowledge_bases=facts.has_knowledge_bases and runs("lookup_knowledge"),
    )


async def execute_gated(
    executor: ToolExecutor, call: ToolCall, scope: TemplateScope, gate: asyncio.Semaphore
) -> ToolAnswer:
    """Run one call (an exception answers ``tool_failed``)."""
    async with gate:
        try:
            return await executor.execute(call, scope)
        except Exception as exc:
            _log.warning("tool_failed", tool=call.name, error_type=type(exc).__name__)
            return ToolAnswer(TOOL_FAILED)


async def stop_tasks(tasks: Iterable[asyncio.Task[Any]]) -> None:
    """Cancel the unfinished ``tasks`` and wait until they ended."""
    running = [task for task in tasks if not task.done()]
    for task in running:
        task.cancel()
    if running:
        await asyncio.wait(running)


def start_tools(
    executor: ToolExecutor | None, planned: Sequence[PlannedCall], scope: TemplateScope
) -> dict[str, asyncio.Task[ToolAnswer]]:
    """One task per executed call (keyed by call id), all rendered with a copy of ``scope``'s variables."""
    if executor is None or not planned:
        return {}
    gate = asyncio.Semaphore(EXECUTOR_CONCURRENCY)
    frozen = dataclasses.replace(scope, variables=dict(scope.variables))
    return {
        item.call.id: asyncio.create_task(
            execute_gated(executor, item.call, frozen, gate), name=f"tool-{item.call.name}"
        )
        for item in planned
    }


def merge_variables(
    variables: MutableMapping[str, Any],
    planned: Sequence[PlannedCall],
    results: Mapping[str, ToolAnswer],
) -> bool:
    """Merge the round's extracted variables in call order; whether anything was merged."""
    changed = False
    for item in planned:
        answer = results.get(item.call.id)
        if answer is not None and answer.variables:
            extracted: Mapping[str, JsonValue] = answer.variables
            variables.update(extracted)
            changed = True
    return changed


class ToolLines:
    """The line spoken while a round's tools run (one per round; ``enabled=False`` silences them all).

    Holds no reference to its owner (a runner or engine), so a finished conversation is freed by reference
    counting.
    """

    def __init__(self, executor: ToolExecutor | None, *, clock: Clock, enabled: bool) -> None:
        self._executor = executor
        self._clock = clock
        self._enabled = enabled
        self._fillers = 0

    async def line(
        self,
        planned: Sequence[PlannedCall],
        tasks: Mapping[str, asyncio.Task[ToolAnswer]],
        scope: TemplateScope,
    ) -> str | None:
        """Tool speech, else a filler when a tool still runs after :data:`FILLER_DELAY_S`, else ``None``."""
        if not self._enabled or not tasks:
            return None
        speech = self._speech(planned, scope)
        if speech is not None:
            return speech
        if not await self._still_running(tasks.values()):
            return None
        filler = TOOL_FILLERS[self._fillers % len(TOOL_FILLERS)]
        self._fillers += 1
        return filler

    def _speech(self, planned: Sequence[PlannedCall], scope: TemplateScope) -> str | None:
        """The rendered ``speech`` of the first executed call whose tool has one (arguments: ``input.*``)."""
        executor = self._executor
        if executor is None:
            return None
        for item in planned:
            template = executor.speech(item.call.name)
            if template is None:
                continue
            arguments = dataclasses.replace(scope, input={"input": call_arguments(item.call)})
            try:
                text = render(template, arguments).strip()
            except TemplateSyntaxError:
                _log.warning("tool_speech_invalid", tool=item.call.name)
                return None
            return text or None
        return None

    async def _still_running(self, tasks: Iterable[asyncio.Task[ToolAnswer]]) -> bool:
        """Wait up to :data:`FILLER_DELAY_S` (on the clock) for ``tasks``; whether any still runs."""
        pending = {task for task in tasks if not task.done()}
        if not pending:
            return False
        delay = asyncio.create_task(self._clock.sleep(FILLER_DELAY_S), name="tool-filler-delay")
        try:
            while pending and not delay.done():
                await asyncio.wait({*pending, delay}, return_when=asyncio.FIRST_COMPLETED)
                pending = {task for task in pending if not task.done()}
        finally:
            await stop_tasks((delay,))
        return bool(pending)
