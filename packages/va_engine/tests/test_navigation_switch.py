"""Transfer Pathway graph switch and return (specs/11 §4, Decision P07-T07)."""

from __future__ import annotations

import uuid

from graph_helpers import graph, node, start
from navigation_helpers import P1, P2, MemoryLoader, active, nav_state, other_graph
from service_helpers import json_log_stream, log_events

from va_engine.models import SwitchGraph
from va_engine.models.graph import PathwayGraph
from va_engine.models.state import NodeFrame
from va_engine.navigation import Navigator


def source_graph() -> PathwayGraph:
    """Pathway ``P1``: greet, the Transfer Pathway node ``hop``, and ``bye``."""
    return graph(
        [
            start("greet"),
            node("hop", node_type="Transfer Pathway", transferPathway={"pathwayId": str(P2), "version": 3}),
            node("bye", node_type="End Call"),
        ]
    )


def loader() -> MemoryLoader:
    return MemoryLoader(
        {(str(P2), 3): active(other_graph(), P2, 3), (str(P1), 1): active(source_graph(), P1, 1)}
    )


def at_hop(memory: MemoryLoader | None = None) -> Navigator:
    state = nav_state("hop")
    state.variables = {"first_name": "Ada"}
    state.visit_counts = {"hop": 1}
    return Navigator(state, active(source_graph()), loader=memory if memory is not None else loader())


def switch(*, node_id: str | None = None, carry: bool = True, back: bool = False) -> SwitchGraph:
    return SwitchGraph(
        pathway_id=str(P2), version=3, node_id=node_id, carry_variables=carry, return_on_end=back
    )


async def test_switch_enters_the_target_start_node_and_keeps_variables() -> None:
    nav = at_hop()

    transition = await nav.switch_graph(switch())

    assert (transition.kind, transition.from_node, transition.to_node) == ("switch", "hop", "billing")
    assert transition.pathway_changed is True
    assert transition.speaks is True
    assert transition.visits == 1
    assert (nav.state.pathway_id, nav.state.pathway_version, nav.state.current_node_id) == (P2, 3, "billing")
    assert nav.active.pathway_id == P2
    assert nav.state.variables == {"first_name": "Ada"}
    assert nav.state.return_stack == []
    assert nav.pathway_return_pending() is False


async def test_switch_to_a_named_node_without_variables() -> None:
    nav = at_hop()

    transition = await nav.switch_graph(switch(node_id="refund", carry=False))

    assert transition.to_node == "refund"
    assert nav.state.variables == {}


async def test_return_on_end_pushes_a_transfer_pathway_frame_and_returns() -> None:
    memory = loader()
    nav = at_hop(memory)
    await nav.switch_graph(switch(back=True))
    nav.enter("refund")

    assert nav.state.return_stack == [
        NodeFrame(kind="transfer_pathway", pathway_id=P1, version=1, node_id="hop")
    ]
    assert nav.pathway_return_pending() is True

    transition = await nav.return_from_pathway()

    assert (transition.kind, transition.from_node, transition.to_node) == ("return", "refund", "hop")
    assert transition.pathway_changed is True
    assert transition.speaks is False
    assert (nav.state.pathway_id, nav.state.pathway_version, nav.state.current_node_id) == (P1, 1, "hop")
    assert nav.state.visit_counts["hop"] == 1
    assert nav.state.return_stack == []
    assert memory.loads == [(str(P2), 3), (str(P1), 1)]


async def test_auto_return_of_a_global_never_pops_the_pathway_frame() -> None:
    nav = at_hop()
    await nav.switch_graph(switch(back=True))

    assert nav.pathway_return_pending() is True
    nav.state.return_stack.append(NodeFrame(kind="global", pathway_id=P2, version=3, node_id="billing"))
    assert nav.pathway_return_pending() is False


async def test_return_within_the_same_pathway_needs_no_load() -> None:
    memory = loader()
    state = nav_state("bye")
    state.return_stack = [NodeFrame(kind="transfer_pathway", pathway_id=P1, version=1, node_id="hop")]
    nav = Navigator(state, active(source_graph()), loader=memory)

    transition = await nav.return_from_pathway()

    assert (transition.kind, transition.to_node, transition.pathway_changed) == ("return", "hop", False)
    assert memory.loads == []


async def test_return_without_a_frame_stays() -> None:
    nav = at_hop()
    transition = await nav.return_from_pathway()
    assert (transition.kind, transition.problem) == ("stay", None)


async def test_return_to_an_unloadable_pathway_drops_the_frame() -> None:
    memory = loader()
    memory.fail.add(str(P1))
    nav = at_hop(memory)
    await nav.switch_graph(switch(back=True))

    stream = json_log_stream()
    transition = await nav.return_from_pathway()

    assert (transition.kind, transition.problem) == ("stay", "return_target_missing")
    assert nav.state.return_stack == []
    assert nav.state.current_node_id == "billing"
    reasons = [
        event["reason"] for event in log_events(stream) if event["event"] == "engine.pathway_load_failed"
    ]
    assert reasons == ["load_failed"]


async def test_return_frame_without_a_version_cannot_be_loaded() -> None:
    state = nav_state("billing", pathway_id=P2, version=3)
    state.return_stack = [NodeFrame(kind="transfer_pathway", pathway_id=None, version=None, node_id="hop")]
    nav = Navigator(state, active(other_graph(), P2, 3), loader=loader())

    assert (await nav.return_from_pathway()).problem == "return_target_missing"


async def test_return_to_a_node_that_no_longer_exists() -> None:
    state = nav_state("billing", pathway_id=P2, version=3)
    state.return_stack = [NodeFrame(kind="transfer_pathway", pathway_id=P1, version=1, node_id="gone")]
    nav = Navigator(state, active(other_graph(), P2, 3), loader=loader())

    assert (await nav.return_from_pathway()).problem == "return_target_missing"
    assert nav.state.pathway_id == P2


async def test_switch_without_a_loader_or_with_a_failing_one_stays() -> None:
    no_loader = Navigator(nav_state("hop"), active(source_graph()))
    assert (await no_loader.switch_graph(switch())).problem == "pathway_unavailable"

    memory = loader()
    memory.fail.add(str(P2))
    failing = at_hop(memory)
    transition = await failing.switch_graph(switch(back=True))
    assert (transition.kind, transition.problem) == ("stay", "pathway_unavailable")
    assert failing.state.return_stack == []
    assert failing.state.variables == {"first_name": "Ada"}


async def test_switch_to_a_missing_node_or_a_graph_without_start_stays() -> None:
    nav = at_hop()
    assert (await nav.switch_graph(switch(node_id="ghost"))).problem == "forward_target_missing"
    assert nav.state.pathway_id == P1

    unknown = uuid.uuid4()
    memory = MemoryLoader({(str(unknown), 3): active(graph([node("x")]), unknown, 3)})
    no_start = at_hop(memory)
    action = SwitchGraph(pathway_id=str(unknown), version=3)
    assert (await no_start.switch_graph(action)).problem == "forward_target_missing"


async def test_switch_out_of_a_global_node_drops_its_frame() -> None:
    pathway = graph(
        [
            start("greet"),
            node(
                "hop",
                node_type="Transfer Pathway",
                isGlobal=True,
                globalLabel="Caller has a billing question",
            ),
        ]
    )
    state = nav_state("hop")
    state.return_stack = [NodeFrame(kind="global", pathway_id=P1, version=1, node_id="greet")]
    nav = Navigator(state, active(pathway), loader=loader())

    await nav.switch_graph(switch(back=True))

    assert nav.state.return_stack == [
        NodeFrame(kind="transfer_pathway", pathway_id=P1, version=1, node_id="hop")
    ]
