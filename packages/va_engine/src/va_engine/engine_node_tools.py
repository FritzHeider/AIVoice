"""Custom and system tools on pathway reply nodes (specs/13 §4; Decisions P08-T02c1 and P08-FIX-16 under
specs/13 §4).

A reply node (:data:`REPLY_NODE_TYPES`, global nodes included) whose reply streams from the dialogue model
is offered the system tools the conversation allows (:class:`~va_engine.system_tools.SystemToolFacts` with
runner ``pathway``: never ``transfer_call``; ``end_call`` only when ``globalConfig.allowEndCallTool``)
followed by the custom tools of the node's ``tools`` and ``globalConfig.globalTools`` the executor resolves.
:class:`NodeTools` computes that :class:`ToolOffer`; :func:`run_node_round` executes one round of the
model's calls: executor tools run as tasks while the tool speech or a filler plays, their answers and
variables are recorded, and ``press_button`` yields a :class:`~va_engine.models.results.SendDtmfAction`. A
round cut short (barge-in) cancels its tool tasks and answers the unanswered calls ``interrupted``; calls
are tracked by their position in the round, never by the model's call id.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncGenerator, Callable, MutableMapping
from dataclasses import dataclass
from typing import Any, Final

from va_common.logging import get_logger
from va_common.time import Clock
from va_engine.models.graph import PathwayGraph
from va_engine.models.nodes import PathwayNode
from va_engine.models.results import Action, Say, SendDtmfAction
from va_engine.system_tools import SystemToolFacts, is_reserved, system_tools
from va_engine.task_tools import INTERRUPTED, PlannedCall, RoundPlan
from va_engine.templating import TemplateScope
from va_engine.tool_executor import PathwayToolExecutor, ToolAnswer, ToolRef
from va_engine.tool_tasks import (
    ToolLines,
    merge_variables,
    round_contents,
    runnable_facts,
    start_tools,
    stop_tasks,
)
from va_llm.models import ToolCall, ToolDef

__all__ = [
    "DTMF_DURATION_MS",
    "NO_OFFER",
    "NO_SYSTEM_TOOLS",
    "REPLY_NODE_TYPES",
    "NodeTools",
    "RoundOutcome",
    "ToolOffer",
    "node_tool_refs",
    "run_node_round",
]

REPLY_NODE_TYPES: Final[frozenset[str]] = frozenset({"Default", "Knowledge Base"})
DTMF_DURATION_MS: Final = 250
"""Tone length of a ``press_button`` call (the Press Button node default)."""

_log = get_logger(__name__)


def node_tool_refs(node: PathwayNode, graph: PathwayGraph) -> list[ToolRef]:
    """The node's ``tools`` followed by ``globalConfig.globalTools``."""
    return [*node.data.tools, *graph.global_config.global_tools]


def _ref_key(ref: ToolRef) -> str:
    if isinstance(ref, str):
        return ref
    name = ref.get("name")
    return f"inline:{name}" if isinstance(name, str) else "inline:"


NO_SYSTEM_TOOLS: Final = SystemToolFacts(runner="pathway", channel="sms", allow_end_call_tool=False)
"""Facts that allow no system tool (a reply offering only custom tools, or none)."""


@dataclass(frozen=True, slots=True)
class ToolOffer:
    """The tools of one reply: definitions (system first), the facts they were built from, and the custom
    tool names the executor runs."""

    tools: tuple[ToolDef, ...] = ()
    facts: SystemToolFacts = NO_SYSTEM_TOOLS
    custom: frozenset[str] = frozenset()

    def handles(self, name: str) -> bool:
        """Whether the executor runs ``name`` here (an offered custom tool or an offered executor-backed
        system tool, which :func:`~va_engine.task_tools.plan_round` validated first)."""
        return name in self.custom or (is_reserved(name) and any(tool.name == name for tool in self.tools))


NO_OFFER: Final = ToolOffer()


class NodeTools:
    """What a pathway conversation's reply nodes offer (no reference to the engine).

    ``executor`` ``None`` = no custom tools; ``facts`` ``None`` = no system tools (text runs).
    """

    def __init__(
        self,
        executor: PathwayToolExecutor | None,
        facts: SystemToolFacts | None,
        *,
        clock: Clock,
        fillers: bool,
    ) -> None:
        self.executor = executor
        self._facts = None if facts is None else runnable_facts(facts, executor)
        self.lines = ToolLines(executor, clock=clock, enabled=fillers)

    def offer(self, node: PathwayNode, graph: PathwayGraph) -> ToolOffer:
        """The tools a reply at ``node`` offers (:data:`NO_OFFER` for other node types)."""
        if node.type not in REPLY_NODE_TYPES or (self.executor is None and self._facts is None):
            return NO_OFFER
        facts = NO_SYSTEM_TOOLS
        system: tuple[ToolDef, ...] = ()
        if self._facts is not None:
            facts = dataclasses.replace(
                self._facts, runner="pathway", allow_end_call_tool=graph.global_config.allow_end_call_tool
            )
            system = system_tools(facts)
        custom = self._custom(node, graph, {tool.name for tool in system})
        return ToolOffer(
            tools=(*system, *custom), facts=facts, custom=frozenset(tool.name for tool in custom)
        )

    def _custom(self, node: PathwayNode, graph: PathwayGraph, taken: set[str]) -> list[ToolDef]:
        refs = node_tool_refs(node, graph)
        if self.executor is None or not refs:
            return []
        resolved = self.executor.definitions_for(refs)
        missing = len({_ref_key(ref) for ref in refs}) - len(resolved)
        if missing > 0:
            _log.info("engine.tool_unresolved", node_id=node.id, count=missing)
        custom: list[ToolDef] = []
        for tool in resolved:
            if is_reserved(tool.name) or tool.name in taken:
                continue
            taken.add(tool.name)
            custom.append(tool)
        return custom


@dataclass(slots=True)
class RoundOutcome:
    """Filled by :func:`run_node_round`: the round's ``end_call`` (``None`` = none) and how many of the
    round's calls were answered so far (they are answered in call order)."""

    terminal: PlannedCall | None = None
    answered: int = 0


async def run_node_round(  # noqa: PLR0913 - the round, its tools, scope, and the engine's callbacks
    plan: RoundPlan,
    *,
    tools: NodeTools,
    scope: TemplateScope,
    variables: MutableMapping[str, Any],
    next_mark: Callable[[], str],
    answer: Callable[[ToolCall, str], None],
    outcome: RoundOutcome,
) -> AsyncGenerator[Action]:
    """Execute ``plan``: yields the speech/filler line, then one :class:`SendDtmfAction` per ``press_button``
    after every call was answered through ``answer`` (in call order)."""
    tasks: list[asyncio.Task[ToolAnswer]] = []
    try:
        tasks = start_tools(tools.executor, plan.executed, scope)
        line = await tools.lines.line(plan.executed, tasks, scope)
        if line is not None:
            yield Say(text=line, mark=next_mark())
        answers = [await task for task in tasks]
        merge_variables(variables, answers)
        presses: list[str] = []
        for item, content in round_contents(plan, answers):
            answer(item.call, content)
            outcome.answered += 1
            if item.digits is not None:
                presses.append(item.digits)
        outcome.terminal = plan.terminal
        for digits in presses:
            yield SendDtmfAction(digits=digits, duration_ms=DTMF_DURATION_MS)
    finally:
        await stop_tasks(tasks)
        for item in plan.calls[outcome.answered :]:
            answer(item.call, INTERRUPTED)
            outcome.answered += 1
