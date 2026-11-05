"""Custom and system tools on pathway reply nodes (specs/13 §4; Decisions P08-T02c1, P08-FIX-16,
P08-FIX-17 and P08-FIX-19 under specs/13 §4).

A reply node (:data:`REPLY_NODE_TYPES`, global nodes included) whose reply streams from the dialogue model
is offered the system tools the conversation allows (:class:`~va_engine.system_tools.SystemToolFacts` with
runner ``pathway``: never ``transfer_call``; ``end_call`` only when ``globalConfig.allowEndCallTool``)
followed by the custom tools of the node's ``tools`` and ``globalConfig.globalTools`` the executor resolves.
Tools are resolved per node (:meth:`~va_engine.tool_executor.PathwayToolExecutor.for_refs`): a node offers,
speaks for and runs exactly the definitions its own refs name, so two nodes may hold different tools of one
name. :class:`NodeTools` computes that :class:`ToolOffer`; :func:`run_node_round` executes one round of the
model's calls: executor tools run as tasks while the tool speech or a filler plays, then the calls are
answered in call order, their variables merged, and each ``press_button`` yields its
:class:`~va_engine.models.results.SendDtmfAction` right after its own answer, so an answer that reports an
action is on record only once that action was handed over. A round cut short (barge-in, or a reply closed
at a press) cancels its tool tasks and answers the unanswered calls ``interrupted``; calls are tracked by
their position in the round, never by the model's call id.
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
from va_engine.tool_executor import PathwayToolExecutor, ToolAnswer, ToolExecutor, ToolRef
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
    """The tools of one reply: definitions (system first), the facts they were built from, the custom
    tool names the executor runs, and ``executor``: the node's own, which runs the offer's custom and
    executor-backed system tools (``None`` = the conversation has no executor)."""

    tools: tuple[ToolDef, ...] = ()
    facts: SystemToolFacts = NO_SYSTEM_TOOLS
    custom: frozenset[str] = frozenset()
    executor: ToolExecutor | None = None

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
        self.lines = ToolLines(clock=clock, enabled=fillers)

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
        refs = node_tool_refs(node, graph)
        # Also without refs: the node's executor runs the executor-backed system tools.
        scoped = None if self.executor is None else self.executor.for_refs(refs)
        custom = _custom(node, scoped, refs, {tool.name for tool in system})
        return ToolOffer(
            tools=(*system, *custom),
            facts=facts,
            custom=frozenset(tool.name for tool in custom),
            executor=scoped,
        )


def _custom(
    node: PathwayNode, scoped: ToolExecutor | None, refs: list[ToolRef], taken: set[str]
) -> list[ToolDef]:
    """The node executor's definitions that may be offered (not named like a system tool).

    ``engine.tool_unresolved`` counts the refs that produced no definition: unresolved ones and those
    shadowed by an earlier ref with the same tool name.
    """
    if scoped is None or not refs:
        return []
    resolved = scoped.definitions()
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
    """Filled by :func:`run_node_round`: how many of the round's calls were answered so far (in call
    order), and the round's ``end_call`` (``None`` = none), which is set only once every call was answered
    and every press handed over: a round cut short never ends the conversation."""

    terminal: PlannedCall | None = None
    answered: int = 0


async def run_node_round(  # noqa: PLR0913 - the round, its executor, scope, and the engine's callbacks
    plan: RoundPlan,
    *,
    executor: ToolExecutor | None,
    lines: ToolLines,
    scope: TemplateScope,
    variables: MutableMapping[str, Any],
    next_mark: Callable[[], str],
    answer: Callable[[ToolCall, str], None],
    outcome: RoundOutcome,
) -> AsyncGenerator[Action]:
    """Execute ``plan`` with the node's ``executor`` (:attr:`ToolOffer.executor`): yields the speech/filler
    line, then answers the calls in call order through ``answer``, each ``press_button`` followed at once by
    its :class:`SendDtmfAction`. A round closed at a press leaves that press ``sent`` (a yielded action is
    carried out) and answers the calls behind it ``interrupted``."""
    tasks: list[asyncio.Task[ToolAnswer]] = []
    try:
        tasks = start_tools(executor, plan.executed, scope)
        line = await lines.line(executor, plan.executed, tasks, scope)
        if line is not None:
            yield Say(text=line, mark=next_mark())
        answers = [await task for task in tasks]
        merge_variables(variables, answers)
        for item, content in round_contents(plan, answers):
            answer(item.call, content)
            outcome.answered += 1
            if item.digits is not None:
                yield SendDtmfAction(digits=item.digits, duration_ms=DTMF_DURATION_MS)
        outcome.terminal = plan.terminal
    finally:
        await stop_tasks(tasks)
        for item in plan.calls[outcome.answered :]:
            answer(item.call, INTERRUPTED)
            outcome.answered += 1
