"""Executor tool rounds shared by the task-mode runner and the pathway engine (specs/13 §5; Decisions
P08-T02b1, P08-T02c1, P08-FIX-16, P08-FIX-17).

The executed calls of one round start as tasks at once (:func:`start_tools`, at most
:data:`~va_engine.task_tools.EXECUTOR_CONCURRENCY` in parallel, all rendered with the variables of the
round's start). :class:`ToolLines` picks what the caller hears while they run: the rendered ``speech`` of the
first executed call whose tool has one (asked of the executor that runs the round: a pathway node's own),
else (when a tool still runs after :data:`~va_engine.task_tools.FILLER_DELAY_S` on the injected clock) one
filler of :data:`~va_voice.phrase_cache.TOOL_FILLERS`, round-robin per conversation.
:func:`merge_variables` merges the round's extractions in call order and :func:`round_contents` pairs every
call of the round with its ``tool`` content.

Tasks, answers and contents belong to a call by its position in the round, never by the model's call id:
providers omit or repeat ids, and two calls sharing one must each run, be cancelled, and be answered.
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
from va_engine.task_tools import (
    EXECUTOR_CONCURRENCY,
    FILLER_DELAY_S,
    TOOL_FAILED,
    PlannedCall,
    RoundPlan,
)
from va_engine.templating import TemplateScope, TemplateSyntaxError, render
from va_engine.tool_executor import PathwayToolExecutor, ToolAnswer, ToolExecutor
from va_llm.models import ToolCall
from va_voice.phrase_cache import TOOL_FILLERS

__all__ = [
    "ToolLines",
    "call_arguments",
    "execute_gated",
    "merge_variables",
    "round_contents",
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


def runnable_facts(
    facts: SystemToolFacts, executor: ToolExecutor | PathwayToolExecutor | None
) -> SystemToolFacts:
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
) -> list[asyncio.Task[ToolAnswer]]:
    """One task per executed call, in the order of ``planned`` (by position, never by call id), all
    rendered with a copy of ``scope``'s variables; ``[]`` without an executor."""
    if executor is None or not planned:
        return []
    gate = asyncio.Semaphore(EXECUTOR_CONCURRENCY)
    frozen = dataclasses.replace(scope, variables=dict(scope.variables))
    return [
        asyncio.create_task(execute_gated(executor, item.call, frozen, gate), name=f"tool-{item.call.name}")
        for item in planned
    ]


def merge_variables(variables: MutableMapping[str, Any], answers: Iterable[ToolAnswer]) -> bool:
    """Merge the variables the round's ``answers`` extracted, in call order; whether anything was merged."""
    changed = False
    for answer in answers:
        if answer.variables:
            extracted: Mapping[str, JsonValue] = answer.variables
            variables.update(extracted)
            changed = True
    return changed


def round_contents(plan: RoundPlan, answers: Sequence[ToolAnswer]) -> list[tuple[PlannedCall, str]]:
    """Every call of ``plan`` in call order with its ``tool`` content.

    An executed call takes the next of ``answers`` (the results of :func:`start_tools`' tasks, by position);
    the others keep their planned answer. Blank content reads ``tool_failed``.
    """
    if len(answers) != len(plan.executed):
        msg = f"{len(plan.executed)} executed calls need as many answers, got {len(answers)}"
        raise ValueError(msg)
    remaining = iter(answers)
    return [
        (item, (next(remaining).content if item.kind == "execute" else item.answer) or TOOL_FAILED)
        for item in plan.calls
    ]


def _speech(
    executor: ToolExecutor | None, planned: Sequence[PlannedCall], scope: TemplateScope
) -> str | None:
    """The rendered ``speech`` of the first executed call whose tool has one (arguments: ``input.*``)."""
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


class ToolLines:
    """The line spoken while a round's tools run (one per round; ``enabled=False`` silences them all).

    Holds no reference to its owner (a runner or engine), so a finished conversation is freed by reference
    counting.
    """

    def __init__(self, *, clock: Clock, enabled: bool) -> None:
        self._clock = clock
        self._enabled = enabled
        self._fillers = 0

    async def line(
        self,
        executor: ToolExecutor | None,
        planned: Sequence[PlannedCall],
        tasks: Sequence[asyncio.Task[ToolAnswer]],
        scope: TemplateScope,
    ) -> str | None:
        """Tool speech (of ``executor``, which runs the round), else a filler when a tool still runs after
        :data:`FILLER_DELAY_S`, else ``None``."""
        if not self._enabled or not tasks:
            return None
        speech = _speech(executor, planned, scope)
        if speech is not None:
            return speech
        if not await self._still_running(tasks):
            return None
        filler = TOOL_FILLERS[self._fillers % len(TOOL_FILLERS)]
        self._fillers += 1
        return filler

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
