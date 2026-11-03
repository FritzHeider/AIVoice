"""Pathway routing (specs/11 §5.2, §5.5; specs/10 §9.1; specs/11 §5.5 Decision P07-T04)."""

from __future__ import annotations

from typing import Any

import pytest
from decision_helpers import CALL_ID, ORG_ID, ScriptedPort, inputs, invalid, unavailable
from graph_helpers import edge, graph, node, start
from service_helpers import json_log_stream, log_events

from va_engine.models.graph import PathwayGraph
from va_engine.models.nodes import dump_edge, dump_node
from va_engine.router import RouteDecision, Router, ScoredOption, heuristic_route, render_router_prompt

ORDER_LABEL = "Caller wants to order food for delivery"
HOURS_LABEL = "Caller asks about opening hours"


def restaurant(**greet: Any) -> PathwayGraph:
    return graph(
        [
            start("greet", name="Greeting", prompt="Greet the caller and ask how you can help.", **greet),
            node("order", name="Take Order"),
            node("hours", name="Hours"),
            node("human", name="Human", isGlobal=True, globalLabel="Caller asks for a human"),
            node("fallback", name="Fallback"),
        ],
        [
            edge("e_order", "greet", "order", ORDER_LABEL, description="Pickup counts too"),
            edge("e_hours", "greet", "hours", HOURS_LABEL),
        ],
    )


def with_fallback(base: PathwayGraph) -> PathwayGraph:
    fallback = edge("e_fb", "greet", "fallback", "", isFallback=True)
    return graph([dump_node(n) for n in base.nodes], [*(dump_edge(e) for e in base.edges), fallback])


def reply(path_id: str, confidence: int, *alternatives: tuple[str, int], reason: str = "r") -> dict[str, Any]:
    return {
        "path_id": path_id,
        "confidence": confidence,
        "alternatives": [{"path_id": p, "confidence": c} for p, c in alternatives],
        "reason": reason,
    }


async def choose(
    pathway: PathwayGraph,
    *script: dict[str, Any] | Exception,
    latest: str = "hello",
    include_globals: bool = False,
    node_id: str = "greet",
) -> tuple[RouteDecision, ScriptedPort]:
    port = ScriptedPort(list(script))
    current = pathway.node(node_id)
    assert current is not None
    decision = await Router(port, clock=port.clock).choose(
        pathway, current, inputs(latest), include_globals=include_globals
    )
    return decision, port


async def test_always_pick_edge_skips_the_model() -> None:
    pathway = graph(
        [start("a"), node("b"), node("c")], [edge("e1", "a", "b"), edge("e2", "a", "c", alwaysPick=True)]
    )
    decision, port = await choose(pathway, node_id="a")
    assert (decision.kind, decision.to_node, decision.label, decision.llm_called) == (
        "edge",
        "c",
        "go",
        False,
    )
    assert port.requests == []


async def test_webhook_edge_skips_the_model() -> None:
    pathway = restaurant()
    port = ScriptedPort()
    greet = pathway.node("greet")
    assert greet is not None
    hours = pathway.edges[1]
    decision = await Router(port, clock=port.clock).choose(pathway, greet, inputs("x"), webhook_edge=hours)
    assert (decision.kind, decision.edge, decision.to_node) == ("edge", hours, "hours")
    assert port.requests == []


async def test_no_candidates_stays_without_the_model() -> None:
    decision, port = await choose(graph([start("a")]), node_id="a")
    assert (decision.kind, decision.to_node, decision.moves) == ("stay", "a", False)
    assert port.requests == []


async def test_only_a_fallback_edge_takes_it_without_the_model() -> None:
    pathway = graph([start("a"), node("b")], [edge("fb", "a", "b", "", isFallback=True)])
    decision, port = await choose(pathway, node_id="a")
    assert (decision.kind, decision.to_node) == ("fallback", "b")
    assert port.requests == []


async def test_chosen_edge_at_five_routes() -> None:
    decision, port = await choose(restaurant(), reply("e_order", 5, ("e_hours", 2)))
    assert (decision.kind, decision.to_node, decision.label) == ("edge", "order", ORDER_LABEL)
    assert decision.alternatives == (ScoredOption(HOURS_LABEL, 2),)
    assert (decision.router_ms, decision.llm_called, decision.degraded) == (250, True, False)
    [request] = port.requests
    assert (request.call_type, request.function_name, request.org_id, request.call_id) == (
        "router",
        "select_path",
        ORG_ID,
        CALL_ID,
    )


async def test_chosen_edge_below_five_stays() -> None:
    decision, _ = await choose(restaurant(), reply("e_order", 4))
    assert (decision.kind, decision.to_node) == ("stay", "greet")
    assert decision.alternatives == (ScoredOption(ORDER_LABEL, 4), ScoredOption(HOURS_LABEL, 0))


async def test_below_five_takes_the_fallback_edge() -> None:
    decision, _ = await choose(with_fallback(restaurant()), reply("e_order", 4))
    assert (decision.kind, decision.to_node, decision.edge and decision.edge.id) == (
        "fallback",
        "fallback",
        "e_fb",
    )


async def test_stay_with_a_fallback_edge_takes_it() -> None:
    decision, _ = await choose(with_fallback(restaurant()), reply("stay", 9))
    assert decision.kind == "fallback"


async def test_stay_outscoring_an_edge_stays() -> None:
    decision, _ = await choose(restaurant(), reply("stay", 8, ("e_order", 7)))
    assert decision.kind == "stay"


async def test_tie_between_stay_and_an_edge_stays() -> None:
    decision, _ = await choose(restaurant(), reply("e_order", 7, ("stay", 7)))
    assert decision.kind == "stay"


async def test_an_alternative_outscoring_the_choice_wins() -> None:
    decision, _ = await choose(restaurant(), reply("e_order", 6, ("e_hours", 9)))
    assert decision.to_node == "hours"
    assert decision.alternatives == (ScoredOption(ORDER_LABEL, 6),)


async def test_ties_go_to_priority_then_edge_order() -> None:
    nodes = [start("a"), node("b"), node("c"), node("d")]
    edges = [
        edge("e1", "a", "b", "one"),
        edge("e2", "a", "c", "two", priority=3),
        edge("e3", "a", "d", "three"),
    ]
    decision, _ = await choose(graph(nodes, edges), reply("e1", 7, ("e2", 7), ("e3", 7)), node_id="a")
    assert decision.to_node == "c"
    edges[1]["data"]["priority"] = 0
    decision, _ = await choose(graph(nodes, edges), reply("e3", 7, ("e2", 7), ("e1", 7)), node_id="a")
    assert decision.to_node == "b"


async def test_unknown_and_repeated_ids_are_ignored() -> None:
    decision, _ = await choose(
        restaurant(), reply("e_order", 6, ("nope", 10), ("e_order", 1), ("e_hours", 3))
    )
    assert decision.to_node == "order"
    assert decision.alternatives == (ScoredOption(HOURS_LABEL, 3),)


async def test_global_at_six_beats_a_lower_local_edge() -> None:
    decision, _ = await choose(restaurant(), reply("global:human", 6, ("e_order", 5)), include_globals=True)
    assert (decision.kind, decision.to_node, decision.global_node_id) == ("global", "human", "human")
    assert decision.label == "Caller asks for a human"
    assert decision.alternatives == (ScoredOption(ORDER_LABEL, 5), ScoredOption(HOURS_LABEL, 0))
    assert decision.event_data() == {
        "from_node": "greet",
        "to_node": "human",
        "label": "Caller asks for a human",
        "alternatives": [{"label": ORDER_LABEL, "score": 5}, {"label": HOURS_LABEL, "score": 0}],
        "global": True,
        "router_ms": 250,
    }


async def test_global_tie_with_local_goes_to_the_global_node() -> None:
    decision, _ = await choose(restaurant(), reply("e_order", 8, ("global:human", 8)), include_globals=True)
    assert decision.kind == "global"


async def test_global_below_six_is_ignored() -> None:
    decision, _ = await choose(restaurant(), reply("global:human", 5, ("e_order", 5)), include_globals=True)
    assert (decision.kind, decision.to_node) == ("edge", "order")


async def test_global_below_a_local_edge_is_ignored() -> None:
    decision, _ = await choose(restaurant(), reply("e_order", 9, ("global:human", 7)), include_globals=True)
    assert decision.to_node == "order"


async def test_global_below_stay_is_ignored() -> None:
    decision, _ = await choose(restaurant(), reply("stay", 9, ("global:human", 7)), include_globals=True)
    assert decision.kind == "stay"


async def test_schema_enum_lists_edges_stay_and_globals_in_order() -> None:
    _, port = await choose(restaurant(), reply("stay", 3), include_globals=True)
    enum = port.requests[0].parameters()["properties"]["path_id"]["enum"]
    assert enum == ["e_order", "e_hours", "stay", "global:human"]
    _, port = await choose(restaurant(), reply("stay", 3))
    assert port.requests[0].parameters()["properties"]["path_id"]["enum"] == ["e_order", "e_hours", "stay"]


async def test_globals_are_not_offered_from_a_global_node() -> None:
    pathway = graph(
        [
            start("a"),
            node("g", isGlobal=True, globalLabel="help"),
            node("h", isGlobal=True, globalLabel="bye"),
        ],
        [edge("e1", "g", "a", "done")],
    )
    _, port = await choose(pathway, reply("stay", 2), include_globals=True, node_id="g")
    assert port.requests[0].parameters()["properties"]["path_id"]["enum"] == ["e1", "stay"]


async def test_globals_alone_call_the_router() -> None:
    pathway = graph([start("a"), node("g", isGlobal=True, globalLabel="Caller asks for help")])
    decision, port = await choose(pathway, reply("global:g", 7), include_globals=True, node_id="a")
    assert (decision.kind, decision.to_node) == ("global", "g")
    assert "- id: global:g | condition: Caller asks for help" in port.requests[0].system


def test_prompt_renders_paths_globals_and_examples() -> None:
    examples = [
        {
            "conversation": [
                {"role": "assistant", "content": "Hi!"},
                {"role": "user", "content": "I'd like\npizza"},
            ],
            "chosenLabel": "  caller wants to ORDER food for delivery ",
        },
        {"conversation": [{"role": "user", "content": "hm"}], "chosenLabel": "No such edge"},
        {"conversation": [{"role": "assistant", "content": "Hi"}], "chosenLabel": HOURS_LABEL},
        {"conversation": [{"role": "user", "content": "x"}], "chosenLabel": ""},
    ]
    pathway = restaurant(pathwayExamples=examples)
    greet = pathway.node("greet")
    assert greet is not None
    prompt = render_router_prompt(greet, pathway.edges, [])
    assert prompt.startswith("You are the routing component of a phone conversation flow.")
    assert (
        'Current step: "Greeting"\n'
        "What the current step is for: Greeting: Greet the caller and ask how you can help.\n\n"
        "Paths from the current step:\n"
        f"- id: e_order | condition: {ORDER_LABEL} | description: Pickup counts too\n"
        f"- id: e_hours | condition: {HOURS_LABEL}\n"
        "- id: stay | condition: None of the paths above apply yet. Remain on the current step.\n\n"
        "Paths available from any step:\nNone\n\n"
        'Examples:\nCaller said: "I\'d like pizza" → path e_order\n\nRules:\n'
    ) in prompt
    assert prompt.endswith("Call select_path exactly once.")


def test_prompt_purpose_is_clipped_and_falls_back_to_text_or_name() -> None:
    pathway = graph([start("a", prompt="x" * 400), node("b", text="Static  line"), node("c", prompt="")])
    a, b, c = pathway.nodes
    assert f"What the current step is for: a: {'x' * 300}\n" in render_router_prompt(a, [], [])
    assert "What the current step is for: b: Static line\n" in render_router_prompt(b, [], [])
    assert "What the current step is for: c\n" in render_router_prompt(c, [], [])


async def test_user_message_has_last_eight_turns_known_information_and_latest() -> None:
    lines = [("user", f"u{i}") if i % 2 else ("assistant", f"a{i}") for i in range(10)]
    lines.insert(3, ("tool", "secret tool output"))
    port = ScriptedPort([reply("stay", 1)])
    pathway = restaurant()
    greet = pathway.node("greet")
    assert greet is not None
    variables = {"zip": "94110", "_token": "hidden", "name": "Mike"}
    await Router(port, clock=port.clock).choose(
        pathway, greet, inputs('two\n"slices"', *lines, variables=variables)
    )
    assert port.requests[0].user == (
        "Agent: a2\nCaller: u3\nAgent: a4\nCaller: u5\nAgent: a6\nCaller: u7\nAgent: a8\nCaller: u9\n"
        'Known information: {"name":"Mike","zip":"94110"}\n'
        'Latest caller message: "two "slices""'
    )


@pytest.mark.parametrize("failure", [unavailable(), invalid("select_path")])
async def test_failures_fall_back_to_the_heuristic(failure: Exception) -> None:
    stream = json_log_stream()
    decision, _ = await choose(restaurant(), failure, latest="I want to order some food please")
    assert (decision.kind, decision.to_node, decision.degraded, decision.llm_called) == (
        "edge",
        "order",
        True,
        True,
    )
    assert decision.router_ms == 250
    [event] = [e for e in log_events(stream) if e["event"] == "engine.router_degraded"]
    assert event["reason"] == type(failure).__name__
    assert "food" not in str(event)


async def test_heuristic_without_a_unique_match_stays_or_falls_back() -> None:
    decision, _ = await choose(restaurant(), unavailable(), latest="food")
    assert decision.kind == "stay"
    decision, _ = await choose(with_fallback(restaurant()), unavailable(), latest="food")
    assert decision.kind == "fallback"


def test_heuristic_needs_exactly_one_edge() -> None:
    pathway = graph(
        [start("a"), node("b"), node("c")],
        [edge("e1", "a", "b", "order food now"), edge("e2", "a", "c", "order food later")],
    )
    a = pathway.node("a")
    assert a is not None
    assert heuristic_route(a, pathway.edges, None, "order food").kind == "stay"
    assert heuristic_route(a, pathway.edges, None, "order food later").kind == "stay"
    assert heuristic_route(a, pathway.edges[1:], None, "order food later").to_node == "c"


async def test_stay_event_data() -> None:
    decision, _ = await choose(restaurant(), reply("stay", 9))
    assert decision.event_data() == {
        "from_node": "greet",
        "to_node": "greet",
        "label": None,
        "alternatives": [{"label": ORDER_LABEL, "score": 0}, {"label": HOURS_LABEL, "score": 0}],
        "global": False,
        "router_ms": 250,
    }
