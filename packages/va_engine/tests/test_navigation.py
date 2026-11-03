"""Global nodes, return stack, and forwarding (specs/11 §4 Decision P07-T05)."""

from __future__ import annotations

import io
from typing import Any

import pytest
from graph_helpers import edge, graph, node, start
from navigation_helpers import P1, P2, MemoryLoader, active, global_graph, nav_state, other_graph, route
from service_helpers import json_log_stream, log_events

from va_engine.models.state import NodeFrame
from va_engine.navigation import MAX_RETURN_STACK, Navigator, Transition


def events(stream: io.StringIO, name: str) -> list[dict[str, Any]]:
    return [line for line in log_events(stream) if line.get("event") == name]


def navigator(current: str = "greet", *, loader: MemoryLoader | None = None) -> Navigator:
    return Navigator(nav_state(current), active(), loader=loader)


def frames(nav: Navigator) -> list[tuple[str, str]]:
    return [(frame.kind, frame.node_id) for frame in nav.state.return_stack]


def test_unknown_current_node_is_rejected() -> None:
    with pytest.raises(ValueError, match="nowhere"):
        Navigator(nav_state("nowhere"), active())


def test_corrupted_current_node_raises_lookup_error() -> None:
    nav = navigator()
    nav.state.current_node_id = "gone"

    with pytest.raises(LookupError, match="gone"):
        _ = nav.node


def test_decision_from_another_node_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="applied on node"):
        navigator().apply(route("order", "edge", "confirm"))


def test_stay_changes_nothing() -> None:
    nav = navigator()

    transition = nav.apply(route("greet", "stay"))

    assert transition == Transition(kind="stay", from_node="greet", to_node="greet")
    assert (transition.speaks, transition.over_limit) == (False, False)
    assert (nav.state.current_node_id, nav.state.visit_counts) == ("greet", {})


def test_edge_enters_and_counts_visits() -> None:
    nav = navigator()

    transition = nav.apply(route("greet", "edge", "order"))

    assert (transition.kind, transition.to_node, transition.visits, transition.max_visits) == (
        "enter",
        "order",
        1,
        8,
    )
    assert transition.speaks
    assert nav.state.current_node_id == "order"
    assert nav.state.visit_counts == {"order": 1}
    assert nav.state.return_stack == []


def test_over_limit_after_max_visits() -> None:
    pathway = graph([start("a"), node("b", maxVisits=2)], [edge("e", "a", "b"), edge("back", "b", "a")])
    nav = Navigator(nav_state("a"), active(pathway))
    seen = []
    for _ in range(3):
        seen.append(nav.apply(route("a", "edge", "b")).over_limit)
        nav.apply(route("b", "edge", "a"))

    assert seen == [False, False, True]
    assert nav.state.visit_counts == {"a": 3, "b": 3}


def test_global_entry_pushes_a_frame_of_the_current_pathway() -> None:
    nav = navigator("order")

    transition = nav.apply(route("order", "global", "hours"))

    assert (transition.kind, transition.to_node, transition.visits, transition.speaks) == (
        "global",
        "hours",
        1,
        True,
    )
    assert nav.state.return_stack == [NodeFrame(kind="global", pathway_id=P1, version=1, node_id="order")]


def test_route_to_missing_target_stays() -> None:
    nav = navigator()
    stream = json_log_stream()
    transition = nav.apply(route("greet", "global", "ghost"))

    assert (transition.kind, transition.problem) == ("stay", "target_missing")
    assert nav.state.return_stack == []
    assert [e["reason"] for e in events(stream, "engine.navigation_failed")] == ["target_missing"]


def test_enter_missing_node_stays() -> None:
    nav = navigator()

    assert nav.enter("ghost").problem == "target_missing"
    assert nav.state.current_node_id == "greet"


@pytest.mark.parametrize(
    ("current", "enabled"), [("greet", True), ("order", True), ("hours", False), ("human", False)]
)
def test_global_check_runs_only_off_global_nodes(current: str, enabled: bool) -> None:
    assert navigator(current).global_check_enabled() is enabled


def test_global_check_needs_another_global_node() -> None:
    no_globals = graph([start("a"), node("b")], [edge("e", "a", "b")])
    assert Navigator(nav_state("a"), active(no_globals)).global_check_enabled() is False


async def test_auto_return_pops_without_speaking_or_counting() -> None:
    nav = navigator("order")
    nav.apply(route("order", "global", "hours"))
    counts = dict(nav.state.visit_counts)

    transition = await nav.finish_node()

    assert transition == Transition(kind="return", from_node="hours", to_node="order")
    assert transition.speaks is False
    assert nav.state.current_node_id == "order"
    assert nav.state.return_stack == []
    assert nav.state.visit_counts == counts


async def test_global_without_auto_return_stays_until_an_edge_leaves() -> None:
    nav = navigator("order")
    nav.apply(route("order", "global", "human"))

    stayed = await nav.finish_node()
    left = nav.apply(route("human", "edge", "goodbye"))

    assert (stayed.kind, stayed.problem) == ("stay", None)
    assert (left.kind, left.to_node) == ("enter", "goodbye")
    assert nav.state.return_stack == []


def test_fallback_from_a_global_node_drops_its_frame() -> None:
    nav = navigator("order")
    nav.apply(route("order", "global", "human"))

    transition = nav.apply(route("human", "fallback", "greet"))

    assert (transition.kind, transition.to_node) == ("enter", "greet")
    assert nav.state.return_stack == []


async def test_global_reached_by_edge_has_no_frame_to_return_to() -> None:
    pathway = graph(
        [start("a"), node("g", isGlobal=True, globalLabel="anything")], [edge("e", "a", "g", "go global")]
    )
    nav = Navigator(nav_state("a"), active(pathway))
    nav.apply(route("a", "edge", "g"))

    transition = await nav.finish_node()

    assert (transition.kind, transition.problem, nav.state.current_node_id) == ("stay", None, "g")


async def test_auto_return_never_pops_a_transfer_pathway_frame() -> None:
    nav = navigator("hours")
    transfer = NodeFrame(kind="transfer_pathway", pathway_id=P2, version=2, node_id="billing")
    nav.state.return_stack.append(transfer)

    transition = await nav.finish_node()

    assert transition.kind == "stay"
    assert nav.state.return_stack == [transfer]


async def test_missing_return_target_drops_the_frame_and_stays() -> None:
    nav = navigator("hours")
    nav.state.return_stack.append(NodeFrame(kind="global", pathway_id=P1, version=1, node_id="ghost"))

    stream = json_log_stream()
    transition = await nav.finish_node()

    assert (transition.kind, transition.problem) == ("stay", "return_target_missing")
    assert nav.state.return_stack == []
    assert nav.state.current_node_id == "hours"
    assert [e["reason"] for e in events(stream, "engine.navigation_failed")] == ["return_target_missing"]


async def test_return_frame_of_another_pathway_is_not_followed() -> None:
    nav = navigator("hours")
    nav.state.return_stack.append(NodeFrame(kind="global", pathway_id=P2, version=1, node_id="greet"))

    transition = await nav.finish_node()

    assert transition.problem == "return_target_missing"
    assert nav.state.current_node_id == "hours"


async def test_forwarding_from_a_global_drops_its_frame_and_speaks() -> None:
    nav = navigator("confirm")
    nav.apply(route("confirm", "global", "menu"))

    transition = await nav.finish_node()

    assert (transition.kind, transition.to_node, transition.speaks, transition.visits) == (
        "forward",
        "order",
        True,
        1,
    )
    assert nav.state.current_node_id == "order"
    assert nav.state.return_stack == []


async def test_forwarding_from_a_regular_node() -> None:
    pathway = graph([start("a", enableForwarding=True, forwardingNode="b"), node("b")], [edge("e", "a", "b")])
    nav = Navigator(nav_state("a"), active(pathway))

    transition = await nav.finish_node()

    assert (transition.kind, transition.from_node, transition.to_node) == ("forward", "a", "b")


async def test_forwarding_disabled_or_without_target_stays() -> None:
    pathway = graph(
        [start("a", enableForwarding=False, forwardingNode="b"), node("b"), node("c", enableForwarding=True)],
        [edge("e", "a", "b")],
    )
    first = Navigator(nav_state("a"), active(pathway))
    second = Navigator(nav_state("c"), active(pathway))

    assert (await first.finish_node()).kind == "stay"
    assert (await second.finish_node()).kind == "stay"


async def test_forwarding_to_a_missing_node_keeps_the_frame() -> None:
    pathway = graph(
        [
            start("a"),
            node("g", isGlobal=True, globalLabel="x", enableForwarding=True, forwardingNode="ghost"),
            node("h", isGlobal=True, globalLabel="y"),
        ]
    )
    nav = Navigator(nav_state("a"), active(pathway))
    nav.apply(route("a", "global", "g"))

    transition = await nav.finish_node()

    assert (transition.kind, transition.problem) == ("stay", "forward_target_missing")
    assert frames(nav) == [("global", "a")]


async def test_forwarding_into_another_pathway() -> None:
    loader = MemoryLoader.with_other()
    nav = navigator("order", loader=loader)
    nav.state.variables["party"] = 4
    nav.apply(route("order", "global", "other_pathway"))

    transition = await nav.finish_node()

    assert (transition.kind, transition.to_node, transition.pathway_changed, transition.speaks) == (
        "forward",
        "billing",
        True,
        True,
    )
    assert loader.loads == [(str(P2), 3)]
    assert (nav.state.pathway_id, nav.state.pathway_version, nav.state.current_node_id) == (P2, 3, "billing")
    assert nav.active.pathway_id == P2
    assert nav.graph.node("refund") is not None
    assert nav.state.return_stack == []
    assert nav.state.variables == {"party": 4}
    assert nav.state.graph_stack_versions == {}


async def test_forwarding_into_another_pathway_at_a_named_node() -> None:
    pathway = graph(
        [
            start(
                "a",
                enableForwarding=True,
                forwardingNode={"pathway_id": str(P2), "version": "staging", "node_id": "refund"},
            )
        ]
    )
    loader = MemoryLoader({(str(P2), "staging"): active(other_graph(), P2, 7)})
    nav = Navigator(nav_state("a"), active(pathway), loader=loader)

    transition = await nav.finish_node()

    assert (transition.to_node, nav.state.pathway_version) == ("refund", 7)


@pytest.mark.parametrize(
    ("target", "loader", "problem"),
    [
        ({"pathway_id": str(P2), "version": 3}, None, "pathway_unavailable"),
        ({"pathway_id": str(P2), "version": 4}, MemoryLoader.with_other(), "pathway_unavailable"),
        (
            {"pathway_id": str(P2), "version": 3, "node_id": "ghost"},
            MemoryLoader.with_other(),
            "forward_target_missing",
        ),
    ],
)
async def test_broken_pathway_forwarding_stays(
    target: dict[str, object], loader: MemoryLoader | None, problem: str
) -> None:
    pathway = graph([start("a", enableForwarding=True, forwardingNode=target)])
    nav = Navigator(nav_state("a"), active(pathway), loader=loader)

    stream = json_log_stream()
    transition = await nav.finish_node()

    assert (transition.kind, transition.problem) == ("stay", problem)
    assert (nav.state.pathway_id, nav.state.current_node_id) == (P1, "a")
    assert [e["reason"] for e in events(stream, "engine.navigation_failed")] == [problem]


async def test_loader_failure_is_logged_with_its_reason() -> None:
    loader = MemoryLoader.with_other()
    loader.fail.add(str(P2))
    nav = navigator("order", loader=loader)
    nav.apply(route("order", "global", "other_pathway"))

    stream = json_log_stream()
    transition = await nav.finish_node()

    assert transition.problem == "pathway_unavailable"
    assert frames(nav) == [("global", "order")]
    assert [e["reason"] for e in events(stream, "engine.pathway_load_failed")] == ["load_failed"]


async def test_target_pathway_without_start_node() -> None:
    no_start = graph([node("x")])
    loader = MemoryLoader({(str(P2), "production"): active(no_start, P2, 1)})
    pathway = graph([start("a", enableForwarding=True, forwardingNode={"pathway_id": str(P2)})])
    nav = Navigator(nav_state("a"), active(pathway), loader=loader)

    assert (await nav.finish_node()).problem == "forward_target_missing"


def test_stack_cap_drops_the_bottom_frame() -> None:
    nav = navigator()
    stack = nav.state.return_stack
    stack.extend(
        NodeFrame(kind="transfer_pathway", pathway_id=P2, version=1, node_id=f"n{index}")
        for index in range(MAX_RETURN_STACK)
    )

    stream = json_log_stream()
    nav.apply(route("greet", "global", "hours"))

    assert len(stack) == MAX_RETURN_STACK
    assert stack[0].node_id == "n1"
    assert (stack[-1].kind, stack[-1].node_id) == ("global", "greet")
    assert len(events(stream, "engine.return_stack_trimmed")) == 1


def test_prev_node_prompt_while_a_global_runs() -> None:
    nav = navigator()
    assert nav.prev_node_prompt() is None

    nav.apply(route("greet", "global", "hours"))
    assert nav.prev_node_prompt() == "Greet the caller."


@pytest.mark.parametrize(
    ("previous", "expected"),
    [({"text": "Static line."}, "Static line."), ({"text": None, "prompt": None}, "")],
)
def test_prev_node_prompt_falls_back_to_text(previous: dict[str, object], expected: str) -> None:
    pathway = graph(
        [
            start("a", **previous),
            node("g", isGlobal=True, globalLabel="x"),
            node("h", isGlobal=True, globalLabel="y"),
        ]
    )
    nav = Navigator(nav_state("a"), active(pathway))
    nav.apply(route("a", "global", "g"))

    assert nav.prev_node_prompt() == expected


@pytest.mark.parametrize(
    "frame",
    [
        NodeFrame(kind="global", pathway_id=P2, version=1, node_id="greet"),
        NodeFrame(kind="global", pathway_id=P1, version=1, node_id="ghost"),
        NodeFrame(kind="transfer_pathway", pathway_id=P1, version=1, node_id="greet"),
    ],
)
def test_prev_node_prompt_needs_a_global_frame_of_this_graph(frame: NodeFrame) -> None:
    nav = navigator("hours")
    nav.state.return_stack.append(frame)

    assert nav.prev_node_prompt() is None


def test_global_graph_is_valid_for_the_scenarios() -> None:
    pathway = global_graph()
    assert [n.id for n in pathway.global_nodes()] == ["hours", "human", "menu", "other_pathway"]
