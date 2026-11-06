"""The pathway turn orchestration (specs/11 §5; Decision P07-T08b under specs/11 §5.9).

:class:`PathwayEngine` drives one conversation through a pathway graph. :meth:`~PathwayEngine.start`,
:meth:`~PathwayEngine.handle_user_turn` and :meth:`~PathwayEngine.on_wait_timeout` are async generators of
the §7 actions; the consumer resumes a generator only after the yielded speech finished playing (text: was
delivered), and closes it on a barge-in, then calls :meth:`~PathwayEngine.on_interrupted`.

A user turn runs one decision pass (extraction, loop condition and router concurrently, plus the speculative
reply at the current node), applies it, and executes the node it lands on (``on_enter`` of a new node, then
``respond``, then forwarding / global auto-return). When the final node does not wait for the caller the
engine chains another pass with an empty message, at most :data:`CHAIN_DEPTH_LIMIT` times per user turn.
Loop protection (``maxVisits``, chain depth, ``maxTotalTurns``) speaks the ``errorPhrase`` and ends the call.
Side effects go through the injected ports; events go to :class:`~va_engine.engine_events.EngineEvents`.

With a :class:`~va_engine.turn_data.TurnData` port the conversation's per-turn dynamic data is refreshed
beside the decision pass of a caller turn (not the opener, a wait timeout, or a chained pass): the decision
sees the earlier values; once decision and refresh both finished, and before the turn's extraction, the
refresh is committed in one synchronous step (its lines, its events and its variables together, so a turn
cut before that step keeps none of them), and a refresh that changed a variable or a context line discards
the speculative reply (specs/13 §7 Decision P08-T03b).

An ended conversation owes its consumer the :class:`~va_engine.models.results.EndCallAction` (Decision
P08-FIX-16 under specs/13 §4): the final lines are not interruptible, and when the reply was closed before
the end was yielded (a caller turn ended before the line's first audio, specs/07 §8) the next caller turn is
not answered but yields the remaining final line and the end.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Coroutine,
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal

from pydantic import JsonValue

from va_common.logging import get_logger
from va_common.time import Clock
from va_domain.call_object import PathwayLog
from va_engine.decisions import DecisionInputs
from va_engine.dialogue import GatewayDialogue
from va_engine.engine_decide import Decider, TurnDecision
from va_engine.engine_events import (
    CONDITION_EVENT,
    EXTRACTION_EVENT,
    ROUTE_EVENT,
    EngineEvents,
    condition_event_data,
    extraction_event_data,
    extraction_worth_reporting,
    handler_route_data,
    report_event,
    route_tag_color,
)
from va_engine.engine_node_tools import NO_OFFER, NodeTools, RoundOutcome, ToolOffer, run_node_round
from va_engine.engine_tools import (
    MAX_SCHEDULING_ROUNDS,
    run_scheduling_calls,
    scheduling_tools,
)
from va_engine.extraction import ExtractionResult
from va_engine.handlers import (
    Channel,
    DefaultHandler,
    HandlerResult,
    LinePort,
    MarkSequence,
    NodeHandler,
    TurnContext,
    UnsupportedNodeError,
    WaitForResponseHandler,
    disarm,
    handler_for,
    speak_node,
)
from va_engine.handlers.base import line_actions
from va_engine.handlers.webhook import node_waits
from va_engine.logs import PathwayLogBook
from va_engine.models.nodes import PathwayNode
from va_engine.models.results import Action, EndCallAction, Say, SayStream, SwitchGraph, is_terminal
from va_engine.models.state import EngineState, TurnRecord, keep_tool_records
from va_engine.navigation import ActiveGraph, Navigator, PathwayLoader, Transition
from va_engine.ports import HttpPort, SmsPort
from va_engine.speculative import Speculation, speculation_allowed
from va_engine.system_tools import EndCallArgs, SystemToolFacts
from va_engine.task_runner import AGENT_END_CALL, DEFAULT_GOODBYE
from va_engine.task_tools import MAX_INVALID_ROUNDS, MAX_TOOL_CALLS_PER_CALL, MAX_TOOL_ROUNDS, plan_round
from va_engine.tool_executor import PathwayToolExecutor
from va_engine.turn_data import TurnData, TurnRefresh, commit_refresh, refresh_turn
from va_llm.models import ToolCall
from va_llm.prompts import INTERRUPTION_MARK, dtmf_message
from va_voice.pipeline.models import UserTurn

__all__ = [
    "CHAIN_DEPTH_LIMIT",
    "LOOP_PROTECTION",
    "ConversationFacts",
    "EnginePorts",
    "PathwayEngine",
    "new_pathway_state",
]

CHAIN_DEPTH_LIMIT: Final = 10
"""Chained passes (nodes that do not wait for the caller) allowed per user turn."""
LOOP_PROTECTION: Final = "loop_protection"

type ProtectionCause = Literal["max_visits", "chain_depth", "max_total_turns"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class EnginePorts:
    """The engine's collaborators (``loader``, ``http``, ``sms`` optional: their nodes report a problem;
    ``tools`` optional: no custom tools are offered without it; ``turn_data`` optional: nothing is refreshed
    per turn without it)."""

    decider: Decider
    dialogue: GatewayDialogue
    lines: LinePort
    events: EngineEvents
    loader: PathwayLoader | None = None
    http: HttpPort | None = None
    sms: SmsPort | None = None
    tools: PathwayToolExecutor | None = None
    turn_data: TurnData | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationFacts:
    """What the conversation's templates and decisions see besides the state; ``system_tools`` ``None`` =
    reply nodes offer no system tool (text runs), ``tool_fillers`` = ``engine.tool_fillers``."""

    org_id: uuid.UUID
    call_id: uuid.UUID | None = None
    channel: Channel = "voice"
    request_data: Mapping[str, object] = field(default_factory=dict)
    builtins: Mapping[str, object] = field(default_factory=dict)
    timezone: str = "UTC"
    transfer_list: Mapping[str, str] = field(default_factory=dict)
    system_tools: SystemToolFacts | None = None
    tool_fillers: bool = True


def new_pathway_state(active: ActiveGraph) -> EngineState:
    """A fresh pathway state at the graph's start node (its first visit counted)."""
    starts = active.graph.start_nodes()
    if not starts:
        msg = "the graph has no start node"
        raise ValueError(msg)
    start = starts[0].id
    return EngineState(
        runner="pathway",
        pathway_id=active.pathway_id,
        pathway_version=active.version,
        current_node_id=start,
        visit_counts={start: 1},
    )


class _CustomCodeFallback:
    """Custom Code nodes until phase 08: nothing runs; the node speaks its ``text``/``prompt`` if any."""

    async def on_enter(self, ctx: TurnContext) -> HandlerResult:
        del ctx
        return HandlerResult()

    async def respond(self, ctx: TurnContext) -> HandlerResult:
        return speak_node(ctx, prompt_required=True)

    def waits_for_user(self, node: PathwayNode) -> bool:
        return node_waits(node)


_DEFAULT: Final = DefaultHandler()
_CUSTOM_CODE: Final = _CustomCodeFallback()
_DEGRADED: Final[Mapping[str, NodeHandler]] = {"Knowledge Base": _DEFAULT, "Custom Code": _CUSTOM_CODE}


class _QueueSink:
    """An :class:`~va_engine.ports.ActionSink` feeding the engine's relay queue."""

    def __init__(self, queue: asyncio.Queue[Action]) -> None:
        self._queue = queue

    def queue(self, action: Action) -> None:
        self._queue.put_nowait(action)


def _decision_inputs(
    facts: ConversationFacts, state: EngineState, latest: str, history: Sequence[TurnRecord]
) -> DecisionInputs:
    return DecisionInputs(
        org_id=facts.org_id,
        latest=latest,
        history=tuple(history),
        variables=dict(state.variables),
        call_id=facts.call_id,
    )


def _latest_inputs(facts: ConversationFacts, state: EngineState) -> DecisionInputs:
    """The caller's latest message and the turns before it."""
    history = state.history
    for index in range(len(history) - 1, -1, -1):
        if history[index].role == "user":
            return _decision_inputs(facts, state, history[index].content, history[:index])
    return _decision_inputs(facts, state, "", history)


class _Extraction:
    """The handlers' :class:`~va_engine.ports.ExtractionPort` over the engine's decider. Holds the
    conversation's data only (never the engine: a bound method of it would put every finished conversation on
    a reference cycle, specs/07 §9 Decision P08-FIX-3)."""

    def __init__(self, decider: Decider, state: EngineState, facts: ConversationFacts, clock: Clock) -> None:
        self._decider = decider
        self._state = state
        self._facts = facts
        self._clock = clock

    async def extract(self, ctx: TurnContext) -> ExtractionResult | None:
        return await self._decider.extract(
            ctx.node,
            _latest_inputs(self._facts, self._state),
            now=self._clock.now(),
            timezone=self._facts.timezone,
        )


@dataclass(slots=True)
class _ToolBudget:
    """The tool rounds of one reply pass at a node (``spoke`` = a reply of the pass produced text)."""

    rounds: int = 0
    invalid: int = 0
    spoke: bool = False


@dataclass(slots=True)
class _Run:
    """One user turn's execution: ``done`` after a terminal action, ``rechain`` after a pathway return,
    ``next`` = a node entered by a handler route or a pathway switch (executed next)."""

    done: bool = False
    rechain: bool = False
    next: Transition | None = None

    def over(self) -> bool:
        """Whether the turn ended (read through a call: callees set ``done``)."""
        return self.done


async def _collect(tokens: AsyncIterator[str], parts: list[str]) -> AsyncGenerator[str]:
    """``tokens`` relayed; every delta the consumer received is appended to ``parts``."""
    try:
        async for token in tokens:
            parts.append(token)
            yield token
    finally:
        close = getattr(tokens, "aclose", None)
        if close is not None:
            await close()


async def _cancelled(task: asyncio.Task[Any]) -> None:
    """Cancel ``task`` unless it finished, and wait for it."""
    if task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


class PathwayEngine:
    """One pathway conversation over ``state`` (mutated in place)."""

    def __init__(  # noqa: PLR0913 - the conversation, its graph, ports, facts, clock, and log book
        self,
        state: EngineState,
        active: ActiveGraph,
        *,
        ports: EnginePorts,
        facts: ConversationFacts,
        clock: Clock,
        logbook: PathwayLogBook | None = None,
    ) -> None:
        self._nav = Navigator(state, active, loader=ports.loader)
        self._active = active
        self._logbook = PathwayLogBook() if logbook is None else logbook
        self._ports = ports
        self._facts = facts
        self._clock = clock
        self._dialogue = ports.dialogue
        self._started = clock.monotonic()
        self._marks = MarkSequence()
        self._marks_turn = state.turn_index
        self._spoken: TurnRecord | None = None
        self._turn_starts: dict[int, tuple[int, int]] = {}
        self._records_added = 0
        self._speculation: Speculation | None = None
        self._background: set[asyncio.Task[None]] = set()
        self._extraction = _Extraction(ports.decider, state, facts, clock)
        self._tools = NodeTools(ports.tools, facts.system_tools, clock=clock, fillers=facts.tool_fillers)
        self._offer: ToolOffer = NO_OFFER
        self._tool_calls_used = 0
        self._owed_end: tuple[Action, ...] = ()

    @property
    def state(self) -> EngineState:
        return self._nav.state

    @property
    def navigator(self) -> Navigator:
        return self._nav

    @property
    def logbook(self) -> PathwayLogBook:
        return self._logbook

    def pathway_logs(self) -> list[PathwayLog]:
        """The specs/11 §12 entries of the conversation so far (pathway = the one the engine started on)."""
        return self._logbook.entries(pathway_id=self._active.pathway_id, pathway_version=self._active.version)

    # Public API.

    async def start(self, greeting: UserTurn | None = None) -> AsyncGenerator[Action]:
        """The current node executes as entered; a ``greeting`` is recorded first as the caller's turn."""
        if self.state.ended:
            return
        if greeting is not None:
            self._accept(greeting)
        current = self.state.current_node_id
        entering = Transition(kind="enter", from_node=current, to_node=current)
        async with contextlib.aclosing(self._converse(_Run(), entering=entering)) as actions:
            async for action in actions:
                yield action

    async def handle_user_turn(self, turn: UserTurn) -> AsyncGenerator[Action]:
        """The caller finished a turn: decide, execute, chain.

        After the end nothing is decided or recorded; an end the consumer never received (its reply was
        closed first) is delivered now: the final line again, then the :class:`EndCallAction`."""
        if self.state.ended:
            _log.info("engine.turn_after_end", turn_index=turn.index, owed=bool(self._owed_end))
            for action in self._owed_end:
                if isinstance(action, EndCallAction):
                    self._owed_end = ()
                yield action
            return
        if turn.supersedes is not None:
            self._drop_turn(turn.supersedes)
        disarm(self.state)
        latest = self._accept(turn)
        history = self.state.history[:-1]
        run = _Run()
        if self.state.turn_index > self._nav.graph.global_config.max_total_turns:
            protection = self._protection(self.state.current_node_id, "max_total_turns")
            async with contextlib.aclosing(self._out(run, protection)) as actions:
                async for action in actions:
                    yield action
            return
        reply = self._converse(run, latest=latest, history=history, refresh=True)
        async with contextlib.aclosing(reply) as actions:
            async for action in actions:
                yield action

    async def on_wait_timeout(self) -> AsyncGenerator[Action]:
        """The Wait for Response timer expired: reprompt, end, repeat, or move on (nothing elsewhere)."""
        node = self._nav.node
        handler = self._handler(node)
        if self.state.ended or not isinstance(handler, WaitForResponseHandler):
            return
        timeout = await handler.timeout(self._ctx(node))
        self._absorb(node, timeout.result)
        run = _Run()
        async with contextlib.aclosing(self._out(run, timeout.result.actions)) as actions:
            async for action in actions:
                yield action
        if run.done or timeout.move_to is None:
            return
        entering = self._route_to(node, timeout.move_to)
        if entering is None:
            return
        async with contextlib.aclosing(self._converse(run, entering=entering)) as actions:
            async for action in actions:
                yield action

    async def on_interrupted(self, spoken_text: str) -> None:
        """Keep only what the caller heard of the interrupted line (it already ends with ``—``)."""
        record, self._spoken = self._spoken, None
        if record is None:
            return
        record.content = spoken_text if spoken_text.strip() else INTERRUPTION_MARK
        record.interrupted = True

    def note_assistant(self, text: str) -> None:
        """Record a line the consumer spoke on its own (a failed transfer's message): the history, the log
        book, and a later barge-in trim see it like an engine line."""
        self._remember(text.strip())

    async def settle_background(self) -> None:
        """Wait until the running background extractions merged their values (before persisting state)."""
        while self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    async def aclose(self) -> None:
        """Cancel background extraction and close an open speculation and the dialogue streams."""
        await self._close_speculation()
        tasks = list(self._background)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._dialogue.aclose()

    # Turn flow.

    async def _converse(
        self,
        run: _Run,
        *,
        entering: Transition | None = None,
        latest: str = "",
        history: Sequence[TurnRecord] = (),
        refresh: bool = False,
    ) -> AsyncGenerator[Action]:
        """Execute the entered node (or decide first), then chain while the conversation does not wait;
        ``refresh`` = the first pass is a caller turn's and refreshes the per-turn data."""
        chained = 0
        while True:
            if entering is not None:
                step = self._execute(run, entering)
            else:
                step = self._pass(run, latest, history, refresh=refresh)
            async with contextlib.aclosing(step) as actions:
                async for action in actions:
                    yield action
            if run.done or self.state.ended:
                return
            if not run.rechain and self._waits():
                return
            run.rechain = False
            chained += 1
            if chained > CHAIN_DEPTH_LIMIT:
                protection = self._protection(self.state.current_node_id, "chain_depth")
                async with contextlib.aclosing(self._out(run, protection)) as actions:
                    async for action in actions:
                        yield action
                return
            entering, latest, history, refresh = None, "", list(self.state.history), False

    async def _pass(
        self, run: _Run, latest: str, history: Sequence[TurnRecord], *, refresh: bool = False
    ) -> AsyncGenerator[Action]:
        """One decision pass at the current node, then the execution of where it lands.

        The per-turn refresh is committed only here, in one synchronous step after the decision
        finished: its lines, its events and its variables take effect together or (a turn cut or failed
        before this point) not at all. Committing inside the refresh task would also hide the change from
        the speculation, which snapshots the variables after an await.
        """
        node = self._nav.node
        inputs = self._inputs(latest, history)
        lines = self._turn_lines()
        try:
            decision, refreshed = await self._decide(node, inputs, refresh=refresh)
            changed = commit_refresh(self.state.variables, refreshed)
            changed = changed or self._turn_lines() != lines
            self._schedule_background(node, inputs)
            entering, suffix = self._apply(node, decision)
            await self._resolve_speculation(
                stays=entering is None, prompt_changed=suffix is not None or changed
            )
        finally:
            await self._close_speculation()
        async with contextlib.aclosing(self._execute(run, entering, suffix)) as actions:
            async for action in actions:
                yield action

    async def _decide(
        self, node: PathwayNode, inputs: DecisionInputs, *, refresh: bool
    ) -> tuple[TurnDecision, TurnRefresh | None]:
        """The turn's decision, with the speculative reply and (``refresh``) the per-turn data refresh
        running beside it; what the refresh fetched is returned, not in effect yet."""
        port = self._ports.turn_data if refresh else None
        refreshed: asyncio.Task[TurnRefresh | None] | None = None
        async with asyncio.TaskGroup() as group:
            decided = group.create_task(
                self._ports.decider.decide(
                    self._nav.graph,
                    node,
                    inputs,
                    include_globals=self._nav.global_check_enabled(),
                    now=self._clock.now(),
                    timezone=self._facts.timezone,
                )
            )
            group.create_task(self._speculate(node))
            if port is not None:
                refreshed = group.create_task(refresh_turn(port, self._ctx(node).scope()))
        return decided.result(), (None if refreshed is None else refreshed.result())

    def _turn_lines(self) -> tuple[str, ...]:
        """The context lines of the per-turn data now."""
        port = self._ports.turn_data
        return () if port is None else port.sections()

    def _apply(self, node: PathwayNode, decision: TurnDecision) -> tuple[Transition | None, str | None]:
        """Merge the extraction, emit the decision events, and move (``None`` = stay on ``node``)."""
        if decision.extraction is not None:
            self._merge_extraction(node, decision.extraction)
        if decision.condition is not None:
            self._emit(CONDITION_EVENT, condition_event_data(node.id, decision.condition))
        if decision.held:
            return None, decision.suffix
        route = decision.route
        transition = self._nav.apply(route)
        if route.moves or route.llm_called:
            self._emit(ROUTE_EVENT, route.event_data())
        return (transition if transition.speaks else None), None

    async def _execute(
        self, run: _Run, entering: Transition | None, suffix: str | None = None
    ) -> AsyncGenerator[Action]:
        """Run the current node (``on_enter`` first when it was just entered), then follow handler routes,
        pathway switches and forwarding until a node is left waiting."""
        while not run.over():
            if entering is not None:
                async with contextlib.aclosing(self._enter(run, entering)) as actions:
                    async for action in actions:
                        yield action
                if run.done or run.rechain:
                    return
            if run.next is None:
                async with contextlib.aclosing(self._respond(run, self._nav.node, suffix)) as actions:
                    async for action in actions:
                        yield action
                suffix = None
            if run.over():
                return
            entering = await self._advance(run)
            if entering is None:
                return

    async def _advance(self, run: _Run) -> Transition | None:
        """The node a handler route / switch entered, else forwarding after the node (``None`` = stay)."""
        entering, run.next = run.next, None
        if entering is not None:
            return entering
        finished = await self._nav.finish_node()
        return finished if finished.speaks else None

    async def _enter(self, run: _Run, entering: Transition) -> AsyncGenerator[Action]:
        """Loop protection, the pathway return of an End Call node, then ``on_enter`` of the current node."""
        protection = self._over_limit(entering)
        if protection is not None:
            async with contextlib.aclosing(self._out(run, protection)) as actions:
                async for action in actions:
                    yield action
            return
        node = self._nav.node
        if node.type == "End Call" and self._nav.pathway_return_pending():
            await self._nav.return_from_pathway()
            run.rechain = True
            return
        async with contextlib.aclosing(self._on_enter(run, node)) as actions:
            async for action in actions:
                yield action

    async def _on_enter(self, run: _Run, node: PathwayNode) -> AsyncGenerator[Action]:
        """``on_enter`` (queued speech relayed while it runs), its actions, then its switch or route."""
        handler = self._handler(node, log=True)
        box: list[HandlerResult] = []
        async with contextlib.aclosing(self._relayed(run, handler.on_enter, self._ctx(node), box)) as actions:
            async for action in actions:
                yield action
        result = box[0]
        self._absorb(node, result)
        spoken = [action for action in result.actions if not isinstance(action, SwitchGraph)]
        async with contextlib.aclosing(self._out(run, spoken)) as actions:
            async for action in actions:
                yield action
        if not run.over():
            await self._follow(run, node, result)

    async def _follow(self, run: _Run, node: PathwayNode, result: HandlerResult) -> None:
        """Apply the ``on_enter`` result's pathway switch, else its handler route."""
        switch = next((action for action in result.actions if isinstance(action, SwitchGraph)), None)
        if switch is not None:
            switched = await self._nav.switch_graph(switch)
            if switched.kind == "switch":
                run.next = switched
                return
        self._handler_route(run, node, result)

    async def _respond(self, run: _Run, node: PathwayNode, suffix: str | None) -> AsyncGenerator[Action]:
        """The node's reply; the tool calls of a streamed reply are answered and the node responds again
        (Scheduling: its booking tools; reply nodes: custom and system tools, Decision P08-T02c1)."""
        handler = self._handler(node)
        self._prepare(node, suffix)
        budget = _ToolBudget()
        while True:
            ctx = self._ctx(node)
            result = await handler.respond(ctx)
            self._absorb(node, result)
            async with contextlib.aclosing(self._out(run, result.actions)) as actions:
                async for action in actions:
                    yield action
            if run.done or self._handler_route(run, node, result):
                return
            calls = await self._reply_calls(result)
            if not calls:
                return
            budget.spoke = budget.spoke or bool(self._dialogue.text.strip())
            if node.type == "Scheduling":
                if not await self._scheduling_round(run, ctx, calls):
                    return
                budget.rounds += 1
            else:
                async with contextlib.aclosing(self._node_round(run, node, calls, budget)) as actions:
                    async for action in actions:
                        yield action
                if run.over():
                    return
            if self._tools_exhausted(node, budget):
                self._dialogue.tools = ()

    def _tools_exhausted(self, node: PathwayNode, budget: _ToolBudget) -> bool:
        if node.type == "Scheduling":
            return budget.rounds >= MAX_SCHEDULING_ROUNDS
        return (
            budget.rounds >= MAX_TOOL_ROUNDS
            or budget.invalid >= MAX_INVALID_ROUNDS
            or self._tool_calls_used >= MAX_TOOL_CALLS_PER_CALL
        )

    async def _reply_calls(self, result: HandlerResult) -> list[ToolCall]:
        """The tool calls of a streamed reply that offered tools (empty when there is nothing to answer).

        A reply the model could not give (``unavailable``: the turn ends after the ``errorPhrase``) and a
        reply cut off mid-stream (its calls may be incomplete) are followed by no tool round, as in task
        mode."""
        last = self._dialogue.last
        if not self._dialogue.tools or last is None or self._dialogue.unavailable is not None:
            return []
        if not any(isinstance(action, SayStream) for action in result.actions):
            return []
        if last.error is not None:
            _log.warning("engine.llm_stream_failed", error_type=type(last.error).__name__)
            return []
        return await last.tool_calls()

    async def _scheduling_round(self, run: _Run, ctx: TurnContext, calls: Sequence[ToolCall]) -> bool:
        """Answer a Scheduling reply's calls; False when a failed booking routed away."""
        answered = await run_scheduling_calls(ctx, calls)
        for answer in answered.answers:
            self._record_tool(ctx.node.id, answer.call_id, answer.name, answer.content)
        for report in answered.reports:
            self._emit(*report_event(report))
        if answered.route_to is not None:
            run.next = self._route_to(ctx.node, answered.route_to)
            return False
        return True

    async def _node_round(
        self, run: _Run, node: PathwayNode, calls: Sequence[ToolCall], budget: _ToolBudget
    ) -> AsyncGenerator[Action]:
        """One round of custom/system tool calls at a reply node, then its ``end_call`` if any."""
        offer = self._offer
        plan = plan_round(
            calls, offer.facts, handles=offer.handles, budget=MAX_TOOL_CALLS_PER_CALL - self._tool_calls_used
        )
        self._tool_calls_used += plan.used
        budget.rounds += 1
        budget.invalid += int(plan.invalid)
        ctx = self._ctx(node)
        outcome = RoundOutcome()
        executed = run_node_round(
            plan,
            executor=offer.executor,
            lines=self._tools.lines,
            scope=ctx.scope(),
            variables=self.state.variables,
            next_mark=ctx.next_mark,
            answer=lambda call, content: self._record_tool(node.id, call.id, call.name, content),
            outcome=outcome,
        )
        async with contextlib.aclosing(executed) as produced:
            async for produced_action in produced:
                async with contextlib.aclosing(self._out(run, (produced_action,))) as actions:
                    async for action in actions:
                        yield action
        terminal = outcome.terminal
        if terminal is None or terminal.end is None:
            return
        async with contextlib.aclosing(self._out(run, self._tool_end(ctx, terminal.end, budget))) as actions:
            async for action in actions:
                yield action

    def _tool_end(self, ctx: TurnContext, end: EndCallArgs, budget: _ToolBudget) -> tuple[Action, ...]:
        """``end_call``: its ``final_message`` (else ``Goodbye.`` when the pass said nothing), then the
        end."""
        final = (end.final_message or "").strip() or (None if budget.spoke else DEFAULT_GOODBYE)
        state = self.state
        state.ended = True
        state.end_reason = AGENT_END_CALL
        disarm(state)
        speech, mark = line_actions(ctx, final or None)
        return (*speech, EndCallAction(reason=AGENT_END_CALL, ended_by="agent", after_mark=mark))

    def _record_tool(self, node_id: str, call_id: str, name: str, content: str) -> None:
        self._record(
            TurnRecord(
                role="tool",
                content=content,
                node_id=node_id,
                tool_call_id=call_id,
                tool_name=name,
                at_ms=self._now_ms(),
            )
        )

    # Speech.

    async def _out(self, run: _Run, actions: Iterable[Action]) -> AsyncGenerator[Action]:
        """Yield ``actions`` in order, recording speech; nothing after a terminal action.

        The final line and :class:`EndCallAction` of an ended conversation stay owed until the end is
        yielded (the consumer ends the call on receipt and closes this generator at that yield)."""
        queued = tuple(actions)
        if run.over():
            return
        self._owe(queued)
        for action in queued:
            if isinstance(action, SayStream):
                async with contextlib.aclosing(self._streamed(action)) as spoken:
                    async for item in spoken:
                        yield item
                continue
            if isinstance(action, Say):
                self._remember(action.text)
            elif isinstance(action, EndCallAction):
                self._owed_end = ()
            yield action
            if is_terminal(action):
                run.done = True
                return

    def _owe(self, queued: Sequence[Action]) -> None:
        """Remember the final line and end of ``queued`` when it ends the conversation."""
        if self.state.ended and any(isinstance(action, EndCallAction) for action in queued):
            self._owed_end = tuple(action for action in queued if isinstance(action, Say | EndCallAction))

    async def _streamed(self, action: SayStream) -> AsyncGenerator[Action]:
        """A streamed reply: what the consumer received is recorded (also when the generator is closed); a
        reply without a token because the model was unavailable is followed by the ``errorPhrase``."""
        parts: list[str] = []
        node_id = self.state.current_node_id
        tokens = _collect(action.tokens, parts)
        try:
            yield replace(action, tokens=tokens)
        finally:
            self._remember("".join(parts), node_id=node_id)
            with contextlib.suppress(RuntimeError):
                await tokens.aclose()
        if not parts and self._dialogue.unavailable is not None:
            error = self._error_line(self._nav.node)
            if error is not None:
                self._remember(error.text)
                yield error

    async def _relayed(
        self,
        run: _Run,
        call: Callable[[TurnContext], Coroutine[Any, Any, HandlerResult]],
        ctx: TurnContext,
        box: list[HandlerResult],
    ) -> AsyncGenerator[Action]:
        """Run a handler call while yielding the speech it queues on the sink; its result goes to ``box``."""
        queue: asyncio.Queue[Action] = asyncio.Queue()
        ctx.sink = _QueueSink(queue)
        task = asyncio.create_task(call(ctx))
        getter = asyncio.create_task(queue.get())
        try:
            while True:
                await asyncio.wait((task, getter), return_when=asyncio.FIRST_COMPLETED)
                if not getter.done():
                    break
                queued = [getter.result()]
                getter = asyncio.create_task(queue.get())
                async with contextlib.aclosing(self._out(run, queued)) as actions:
                    async for action in actions:
                        yield action
            await _cancelled(getter)
            rest: list[Action] = []
            while not queue.empty():
                rest.append(queue.get_nowait())
            async with contextlib.aclosing(self._out(run, rest)) as actions:
                async for action in actions:
                    yield action
            box.append(task.result())
        finally:
            await _cancelled(getter)
            await _cancelled(task)

    def _error_line(self, node: PathwayNode) -> Say | None:
        """The rendered ``errorPhrase`` as a line (``None`` when blank)."""
        ctx = self._ctx(node)
        text = ctx.render(self._nav.graph.global_config.error_phrase, field_name="errorPhrase").strip()
        return Say(text=text, mark=ctx.next_mark()) if text else None

    def _protection(self, node_id: str, cause: ProtectionCause) -> tuple[Action, ...]:
        """Loop protection: the ``errorPhrase`` (not interruptible: it is a final line) then the end of the
        call."""
        _log.warning("engine.loop_protection", node_id=node_id, cause=cause)
        state = self.state
        state.ended = True
        state.end_reason = LOOP_PROTECTION
        disarm(state)
        error = self._error_line(self._nav.node)
        if error is not None:
            error = replace(error, interruptible=False)
        end = EndCallAction(
            reason=LOOP_PROTECTION, ended_by="system", after_mark=error.mark if error is not None else None
        )
        return (error, end) if error is not None else (end,)

    def _over_limit(self, entering: Transition) -> tuple[Action, ...] | None:
        """``None`` when the entered node may run (possibly after taking its fallback edge), else the loop
        protection actions."""
        if not entering.over_limit:
            return None
        graph = self._nav.graph
        fallback = next((edge for edge in graph.out_edges(entering.to_node) if edge.data.is_fallback), None)
        if fallback is not None:
            redirected = self._nav.enter(fallback.target)
            if redirected.kind == "enter" and not redirected.over_limit:
                self._emit(ROUTE_EVENT, handler_route_data(graph, entering.to_node, fallback.target))
                return None
        return self._protection(entering.to_node, "max_visits")

    # Routing helpers.

    def _handler_route(self, run: _Run, node: PathwayNode, result: HandlerResult) -> bool:
        """Follow the handler's own route (``route_to``, else ``route_fallback``); True when it moved."""
        target = result.route_to
        if target is None and result.route_fallback:
            fallback = next(
                (edge for edge in self._nav.graph.out_edges(node.id) if edge.data.is_fallback), None
            )
            target = fallback.target if fallback is not None else None
        if target is None:
            return False
        run.next = self._route_to(node, target)
        return run.next is not None

    def _route_to(self, node: PathwayNode, target: str) -> Transition | None:
        """Enter ``target`` from ``node`` and emit its ``call.route`` (``None`` when it is not a node)."""
        entered = self._nav.enter(target)
        if entered.kind == "stay":
            return None
        self._emit(ROUTE_EVENT, handler_route_data(self._nav.graph, node.id, target))
        return entered

    def _waits(self) -> bool:
        node = self._nav.node
        return self._handler(node).waits_for_user(node) and not node.data.model_options.skip_user_response

    def _handler(self, node: PathwayNode, *, log: bool = False) -> NodeHandler:
        try:
            return handler_for(node)
        except UnsupportedNodeError:
            if log:
                _log.info("engine.node_degraded", node_id=node.id, type=node.type)
            return _DEGRADED.get(node.type, _CUSTOM_CODE)

    def _absorb(self, node: PathwayNode, result: HandlerResult) -> None:
        """Emit the handler's reports and log its problem."""
        for report in result.reports:
            self._emit(*report_event(report))
        if result.problem is not None:
            _log.warning("engine.handler_problem", node_id=node.id, problem=result.problem)

    # Decisions.

    def _inputs(self, latest: str, history: Sequence[TurnRecord]) -> DecisionInputs:
        return _decision_inputs(self._facts, self.state, latest, history)

    def _merge_extraction(
        self, node: PathwayNode, result: ExtractionResult, *, turn_index: int | None = None
    ) -> None:
        self.state.variables.update(result.values)
        if extraction_worth_reporting(result):
            self._emit(EXTRACTION_EVENT, extraction_event_data(node.id, result), turn_index=turn_index)

    def _schedule_background(self, node: PathwayNode, inputs: DecisionInputs) -> None:
        """Start the node's ``background`` extraction (merged and reported when it completes)."""
        if not node.data.extract_vars or node.data.extract_vars_mode != "background":
            return
        task = asyncio.create_task(self._background_extraction(node, inputs, self.state.turn_index))
        self._background.add(task)
        task.add_done_callback(lambda done: self._background_done(node.id, done))

    async def _background_extraction(
        self, node: PathwayNode, inputs: DecisionInputs, turn_index: int
    ) -> None:
        """Run a ``background`` extraction; its event keeps the turn index of the pass that started it."""
        result = await self._ports.decider.extract(
            node, inputs, now=self._clock.now(), timezone=self._facts.timezone
        )
        if result is not None:
            self._merge_extraction(node, result, turn_index=turn_index)

    def _background_done(self, node_id: str, task: asyncio.Task[None]) -> None:
        self._background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _log.error(
                "engine.background_extraction_failed", node_id=node_id, error=type(task.exception()).__name__
            )

    async def _speculate(self, node: PathwayNode) -> None:
        settings = self._dialogue.prompt.settings
        if not speculation_allowed(node, self._nav.graph, enabled=settings.speculative):
            return
        self._prepare(node, None)
        self._speculation = await Speculation.start(self._dialogue, self._ctx(node))

    async def _resolve_speculation(self, *, stays: bool, prompt_changed: bool) -> None:
        speculation, self._speculation = self._speculation, None
        if speculation is None:
            return
        try:
            await speculation.resolve(
                stays=stays, variables=self.state.variables, prompt_changed=prompt_changed
            )
        finally:
            await speculation.aclose()

    async def _close_speculation(self) -> None:
        speculation, self._speculation = self._speculation, None
        if speculation is not None:
            await speculation.aclose()

    # State.

    def _prepare(self, node: PathwayNode, suffix: str | None) -> None:
        """The dialogue port's per-reply inputs at ``node``. The call's tool speech is read here, per reply
        pass: a graph the call switched to may have brought the first one (specs/13 §4, Decision P08-T02c2
        item 5)."""
        self._dialogue.condition_suffix = suffix
        speech = None if self._ports.tools is None else self._ports.tools.tool_speech
        if speech:
            self._dialogue.tool_speech = speech
        if node.type == "Scheduling":
            self._offer = NO_OFFER
            self._dialogue.tools = scheduling_tools()
        else:
            self._offer = self._tools.offer(node, self._nav.graph)
            spent = self._tool_calls_used >= MAX_TOOL_CALLS_PER_CALL
            self._dialogue.tools = () if spent else self._offer.tools
        self._dialogue.extra = []

    def _ctx(self, node: PathwayNode) -> TurnContext:
        facts = self._facts
        builtins = dict(facts.builtins)
        previous = self._nav.prev_node_prompt()
        if previous is not None:
            builtins["prevNodePrompt"] = previous
        return TurnContext(
            state=self.state,
            graph=self._nav.graph,
            node=node,
            channel=facts.channel,
            dialogue=self._dialogue,
            lines=self._ports.lines,
            now_ms=self._now_ms,
            request_data=facts.request_data,
            builtins=builtins,
            timezone=facts.timezone,
            transfer_list=facts.transfer_list,
            marks=self._turn_marks(),
            http=self._ports.http,
            sms=self._ports.sms,
            extractor=self._extraction,
            clock_now=self._clock.now,
        )

    def _turn_marks(self) -> MarkSequence:
        """One mark sequence per turn index, so mark names stay unique within a turn."""
        if self.state.turn_index != self._marks_turn:
            self._marks = MarkSequence()
            self._marks_turn = self.state.turn_index
        return self._marks

    def _now_ms(self) -> int:
        return int((self._clock.monotonic() - self._started) * 1000)

    def _accept(self, turn: UserTurn) -> str:
        """Record the caller's turn (keypad turns as ``[Caller pressed: …]``); its text."""
        self.state.turn_index += 1
        # The pipeline only ever supersedes the turn right before the new one: keep that turn's mark alone.
        self._turn_starts = {turn.index: (self._records_added, self.state.turn_index)}
        content = (dtmf_message(turn.dtmf).content or "") if turn.dtmf else turn.text
        self._record(
            TurnRecord(
                role="user", content=content, node_id=self.state.current_node_id, at_ms=int(turn.end_ms)
            )
        )
        return content

    def _drop_turn(self, index: int) -> None:
        """A superseding turn replaces turn ``index``: its user and assistant records go, its ``tool``
        records stay (ahead of the merged turn).

        The tools ran: their side effects and merged variables are not withdrawn, so the next request must
        still see their answers (specs/13 §4 Decision P08-FIX-17). The turn is located by the number of
        records added since, not by a history index: once the history passes ``HISTORY_CAP`` the oldest
        records fold into the summary and every index shifts.
        """
        mark = self._turn_starts.pop(index, None)
        if mark is None:
            return
        added, turn_index = mark
        self._logbook.drop_turn(turn_index)
        self._records_added = added + keep_tool_records(self.state.history, self._records_added - added)
        self._spoken = None

    def _remember(self, text: str, *, node_id: str | None = None) -> None:
        """Record a spoken assistant line (blank text records nothing); a barge-in trims it."""
        if not text:
            return
        record = TurnRecord(
            role="assistant",
            content=text,
            node_id=node_id or self.state.current_node_id,
            at_ms=self._now_ms(),
        )
        self._record(record)
        self._spoken = record

    def _record(self, record: TurnRecord) -> None:
        self.state.add_turn(record)
        self._records_added += 1
        self._logbook.on_record(
            self.state.turn_index, record, node_name=self._node_name(record.node_id), at=self._clock.now()
        )

    def _node_name(self, node_id: str | None) -> str | None:
        node = None if node_id is None else self._nav.graph.node(node_id)
        return None if node is None else node.data.name

    def _emit(self, event_type: str, data: dict[str, JsonValue], *, turn_index: int | None = None) -> None:
        """Hand an event to the sink and the log book, stamped with its ``turn_index`` (routes also carry
        the ``tag_color`` of the node they enter; Decision P07-T08c)."""
        stamped: dict[str, JsonValue] = {
            **data,
            "turn_index": self.state.turn_index if turn_index is None else turn_index,
        }
        if event_type == ROUTE_EVENT:
            stamped["tag_color"] = route_tag_color(self._nav.graph, data.get("to_node"))
        self._logbook.on_event(event_type, stamped)
        self._ports.events.emit(event_type, stamped)
