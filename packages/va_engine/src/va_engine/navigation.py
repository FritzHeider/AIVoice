"""Global nodes, the return stack, and forwarding (specs/11 §2, §3.1, §4, §5.2; Decision P07-T05 under §4).

:class:`Navigator` applies a :class:`~va_engine.router.RouteDecision` to an :class:`EngineState` and moves the
conversation after a node executed (:meth:`Navigator.finish_node`): forwarding to another node or pathway, the
global node auto-return, or staying. It is pure navigation: no events and no LLM calls; the turn orchestration
reads the returned :class:`Transition` (``speaks``, ``over_limit``, ``problem``) and acts on it. Broken
targets never raise: the conversation stays on the current node and :attr:`Transition.problem` says why.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Protocol
from uuid import UUID

from va_common.logging import get_logger
from va_engine.models.graph import PathwayGraph
from va_engine.models.nodes import ForwardingTarget, PathwayNode, PathwayVersionRef
from va_engine.models.state import EngineState, NodeFrame

if TYPE_CHECKING:
    from va_engine.router import RouteDecision

__all__ = [
    "MAX_RETURN_STACK",
    "ActiveGraph",
    "Navigator",
    "PathwayLoader",
    "PathwayUnavailableError",
    "Transition",
    "TransitionKind",
    "TransitionProblem",
]

MAX_RETURN_STACK: Final = 16
"""Deepest return stack; pushing beyond it drops the bottom frame."""

type TransitionKind = Literal["stay", "enter", "global", "return", "forward"]
type TransitionProblem = Literal[
    "target_missing", "forward_target_missing", "return_target_missing", "pathway_unavailable"
]

_SPEAKING: Final = frozenset({"enter", "global", "forward"})

_logger = get_logger(__name__)


class PathwayUnavailableError(Exception):
    """A :class:`PathwayLoader` could not provide the requested pathway version."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"pathway unavailable: {reason}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ActiveGraph:
    """The graph the conversation runs on and the pathway version it came from."""

    pathway_id: UUID | None
    version: int | None
    graph: PathwayGraph


class PathwayLoader(Protocol):
    """Loads another pathway's graph (forwarding to a pathway; the Transfer Pathway node).

    ``load`` returns the resolved version's graph or raises :class:`PathwayUnavailableError`.
    """

    async def load(self, pathway_id: str, version: PathwayVersionRef) -> ActiveGraph: ...


@dataclass(frozen=True, slots=True)
class Transition:
    """What a navigation step did.

    ``enter``/``global``/``forward`` made ``to_node`` current with ``visits`` = its new visit count;
    ``return`` popped a global frame (no visit counted, nothing re-spoken); ``stay`` kept the node
    (``problem`` set when a target was broken).
    """

    kind: TransitionKind
    from_node: str
    to_node: str
    pathway_changed: bool = False
    visits: int = 0
    max_visits: int | None = None
    problem: TransitionProblem | None = None

    @property
    def over_limit(self) -> bool:
        """True when the entered node was visited more often than its ``maxVisits``."""
        return self.max_visits is not None and self.visits > self.max_visits

    @property
    def speaks(self) -> bool:
        """True when the new current node executes (its response is produced)."""
        return self.kind in _SPEAKING


def _stay(node_id: str, problem: TransitionProblem | None = None) -> Transition:
    return Transition(kind="stay", from_node=node_id, to_node=node_id, problem=problem)


class Navigator:
    """Moves ``state`` through ``active`` (other pathways through ``loader``); mutates ``state`` in place."""

    def __init__(
        self, state: EngineState, active: ActiveGraph, *, loader: PathwayLoader | None = None
    ) -> None:
        if active.graph.node(state.current_node_id) is None:
            msg = f"current node {state.current_node_id!r} is not in the graph"
            raise ValueError(msg)
        self.state = state
        self.active = active
        self._loader = loader

    @property
    def graph(self) -> PathwayGraph:
        return self.active.graph

    @property
    def node(self) -> PathwayNode:
        """The current node."""
        current = self.graph.node(self.state.current_node_id)
        if current is None:
            msg = f"current node {self.state.current_node_id!r} is not in the graph"
            raise LookupError(msg)
        return current

    def global_check_enabled(self) -> bool:
        """Whether the §5.2 global check runs: the node is not global and another global node exists."""
        current = self.node
        if current.data.is_global:
            return False
        return any(other.id != current.id for other in self.graph.global_nodes())

    def apply(self, decision: RouteDecision) -> Transition:
        """Follow a router decision (Decision P07-T05 rules 1, 3, 4)."""
        current = self.node
        if decision.from_node != current.id:
            msg = f"decision from {decision.from_node!r} applied on node {current.id!r}"
            raise ValueError(msg)
        if decision.kind == "stay":
            return _stay(current.id)
        if self.graph.node(decision.to_node) is None:
            return self._failed(current.id, "target_missing")
        if decision.kind == "global":
            self._push(
                NodeFrame(
                    kind="global",
                    pathway_id=self.active.pathway_id,
                    version=self.active.version,
                    node_id=current.id,
                )
            )
            return self.enter(decision.to_node, kind="global")
        self._drop_global_frame()
        return self.enter(decision.to_node)

    def enter(self, node_id: str, *, kind: TransitionKind = "enter") -> Transition:
        """Make ``node_id`` current and count the visit (stays with ``target_missing`` for an unknown id)."""
        from_node = self.state.current_node_id
        target = self.graph.node(node_id)
        if target is None:
            return self._failed(from_node, "target_missing")
        visits = self.state.visit_counts.get(node_id, 0) + 1
        self.state.visit_counts[node_id] = visits
        self.state.current_node_id = node_id
        return Transition(
            kind=kind, from_node=from_node, to_node=node_id, visits=visits, max_visits=target.data.max_visits
        )

    async def finish_node(self) -> Transition:
        """Where the conversation goes after the current node executed (Decision P07-T05 rule 2)."""
        current = self.node
        forwarding = current.data.forwarding_node
        if current.data.enable_forwarding and forwarding is not None:
            if isinstance(forwarding, ForwardingTarget):
                return await self._forward_to_pathway(current, forwarding)
            return self._forward(current, forwarding)
        if current.data.is_global and current.data.enable_global_auto_return:
            return self._return(current)
        return _stay(current.id)

    def prev_node_prompt(self) -> str | None:
        """``{{prevNodePrompt}}`` while a global node runs: the prompt (else text) of its return node."""
        frame = self._global_frame()
        if not self.node.data.is_global or frame is None or frame.pathway_id != self.active.pathway_id:
            return None
        previous = self.graph.node(frame.node_id)
        if previous is None:
            return None
        return previous.data.prompt or previous.data.text or ""

    def _forward(self, current: PathwayNode, node_id: str) -> Transition:
        if self.graph.node(node_id) is None:
            return self._failed(current.id, "forward_target_missing")
        self._drop_global_frame()
        return self.enter(node_id, kind="forward")

    async def _forward_to_pathway(self, current: PathwayNode, target: ForwardingTarget) -> Transition:
        if self._loader is None:
            return self._failed(current.id, "pathway_unavailable")
        try:
            active = await self._loader.load(target.pathway_id, target.version)
        except PathwayUnavailableError as error:
            _logger.warning("engine.pathway_load_failed", node_id=current.id, reason=error.reason)
            return self._failed(current.id, "pathway_unavailable")
        node_id = target.node_id
        if node_id is None:
            starts = active.graph.start_nodes()
            node_id = starts[0].id if starts else None
        if node_id is None or active.graph.node(node_id) is None:
            return self._failed(current.id, "forward_target_missing")
        self._drop_global_frame()
        self.active = active
        self.state.pathway_id = active.pathway_id
        self.state.pathway_version = active.version
        entered = self.enter(node_id, kind="forward")
        return Transition(
            kind=entered.kind,
            from_node=current.id,
            to_node=entered.to_node,
            pathway_changed=True,
            visits=entered.visits,
            max_visits=entered.max_visits,
        )

    def _return(self, current: PathwayNode) -> Transition:
        frame = self._global_frame()
        if frame is None:
            return _stay(current.id)
        self.state.return_stack.pop()
        if frame.pathway_id != self.active.pathway_id or self.graph.node(frame.node_id) is None:
            return self._failed(current.id, "return_target_missing")
        self.state.current_node_id = frame.node_id
        return Transition(kind="return", from_node=current.id, to_node=frame.node_id)

    def _global_frame(self) -> NodeFrame | None:
        stack = self.state.return_stack
        return stack[-1] if stack and stack[-1].kind == "global" else None

    def _drop_global_frame(self) -> None:
        if self.node.data.is_global and self._global_frame() is not None:
            self.state.return_stack.pop()

    def _push(self, frame: NodeFrame) -> None:
        stack = self.state.return_stack
        stack.append(frame)
        if len(stack) > MAX_RETURN_STACK:
            del stack[: len(stack) - MAX_RETURN_STACK]
            _logger.warning("engine.return_stack_trimmed", depth=MAX_RETURN_STACK)

    def _failed(self, node_id: str, problem: TransitionProblem) -> Transition:
        _logger.warning("engine.navigation_failed", node_id=node_id, reason=problem)
        return _stay(node_id, problem)
