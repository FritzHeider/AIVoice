"""Navigation test builders: an in-memory pathway loader, states, and a graph with global nodes."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Final

from graph_helpers import edge, graph, node, start

from va_engine.models.graph import PathwayGraph
from va_engine.models.nodes import PathwayVersionRef
from va_engine.models.state import EngineState
from va_engine.navigation import ActiveGraph, PathwayUnavailableError
from va_engine.router import RouteDecision, RouteKind

__all__ = [
    "P1",
    "P2",
    "MemoryLoader",
    "active",
    "global_graph",
    "nav_state",
    "other_graph",
    "route",
]

P1: Final = uuid.UUID("0191f2a4-7c55-7a1e-9a10-5b2e4c8f1b01")
P2: Final = uuid.UUID("0191f2a4-7c55-7a1e-9a10-5b2e4c8f1b02")

ORDER_LABEL: Final = "Caller wants to order"
CONFIRM_LABEL: Final = "Caller finished ordering"
GOODBYE_LABEL: Final = "Caller is done talking to a human"


def global_graph() -> PathwayGraph:
    """greet → order → confirm, with globals ``hours`` (auto-return), ``human`` (no auto-return, edge to
    ``goodbye``), ``menu`` (forwards to ``order``), ``other_pathway`` (forwards into pathway ``P2``)."""
    return graph(
        [
            start("greet", name="Greeting", prompt="Greet the caller."),
            node("order", name="Order", prompt="Take the order."),
            node("confirm", name="Confirm", text="Your order is confirmed."),
            node("goodbye", name="Goodbye"),
            node("hours", name="Hours", isGlobal=True, globalLabel="Caller asks about opening hours"),
            node(
                "human",
                name="Human",
                isGlobal=True,
                globalLabel="Caller asks for a human",
                enableGlobalAutoReturn=False,
            ),
            node(
                "menu",
                name="Menu",
                isGlobal=True,
                globalLabel="Caller asks what is on the menu",
                enableForwarding=True,
                forwardingNode="order",
            ),
            node(
                "other_pathway",
                name="Billing",
                isGlobal=True,
                globalLabel="Caller has a billing question",
                enableForwarding=True,
                forwardingNode={"pathway_id": str(P2), "version": 3},
            ),
        ],
        [
            edge("e_order", "greet", "order", ORDER_LABEL),
            edge("e_confirm", "order", "confirm", CONFIRM_LABEL),
            edge("e_goodbye", "human", "goodbye", GOODBYE_LABEL),
            edge("e_fb", "human", "greet", "", isFallback=True),
        ],
    )


def other_graph() -> PathwayGraph:
    """Pathway ``P2``: billing → refund, with a global ``agent`` (auto-return)."""
    return graph(
        [
            start("billing", name="Billing Start", prompt="Ask about the bill."),
            node("refund", name="Refund"),
            node("agent", name="Agent", isGlobal=True, globalLabel="Caller asks for an agent"),
        ],
        [edge("e_refund", "billing", "refund", "Caller wants a refund")],
    )


def active(
    pathway: PathwayGraph | None = None, pathway_id: uuid.UUID | None = P1, version: int = 1
) -> ActiveGraph:
    return ActiveGraph(pathway_id=pathway_id, version=version, graph=pathway or global_graph())


def nav_state(
    current: str = "greet", pathway_id: uuid.UUID | None = P1, version: int | None = 1
) -> EngineState:
    return EngineState(
        runner="pathway", pathway_id=pathway_id, pathway_version=version, current_node_id=current
    )


def route(from_node: str, kind: RouteKind, to_node: str | None = None) -> RouteDecision:
    """A router decision without edge details (the navigator reads kind and nodes only)."""
    target = from_node if to_node is None else to_node
    return RouteDecision(
        kind=kind,
        from_node=from_node,
        to_node=target,
        global_node_id=target if kind == "global" else None,
    )


@dataclass(slots=True)
class MemoryLoader:
    """A :class:`~va_engine.navigation.PathwayLoader` over a dict; ``fail`` holds pathway ids to refuse."""

    graphs: dict[tuple[str, PathwayVersionRef], ActiveGraph] = field(default_factory=dict)
    fail: set[str] = field(default_factory=set)
    loads: list[tuple[str, PathwayVersionRef]] = field(default_factory=list)

    async def load(self, pathway_id: str, version: PathwayVersionRef) -> ActiveGraph:
        self.loads.append((pathway_id, version))
        if pathway_id in self.fail:
            raise PathwayUnavailableError("load_failed")
        found = self.graphs.get((pathway_id, version))
        if found is None:
            raise PathwayUnavailableError("not_found")
        return found

    @classmethod
    def with_other(cls) -> MemoryLoader:
        return cls({(str(P2), 3): active(other_graph(), P2, 3)})
