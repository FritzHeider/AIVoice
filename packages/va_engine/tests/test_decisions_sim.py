"""Decision calls through the real gateway against FakeLLM (specs/11 §5.5 Decision P07-T04)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from decision_helpers import NOW, inputs
from graph_helpers import edge, graph, node, start
from task_helpers import Sim, served

from va_common.time import FrozenClock
from va_engine.extraction import Extractor
from va_engine.loop_condition import LoopCondition
from va_engine.models.graph import PathwayGraph
from va_engine.router import Router

ORDER_LABEL = "Caller wants to order food"
HOURS_LABEL = "Caller asks about opening hours"


@pytest.fixture
async def sim() -> AsyncIterator[Sim]:
    async with served() as running:
        yield running


def restaurant() -> PathwayGraph:
    return graph(
        [
            start("greet", name="Greeting", condition="The caller said what they want", conditionThreshold=8),
            node("order", name="Take Order"),
            node("hours", name="Hours"),
        ],
        [edge("e_order", "greet", "order", ORDER_LABEL), edge("e_hours", "greet", "hours", HOURS_LABEL)],
    )


def router(sim: Sim) -> Router:
    return Router(sim.rig.gateway, clock=FrozenClock(NOW))


async def test_fakellm_routes_by_label_words(sim: Sim) -> None:
    pathway = restaurant()
    greet = pathway.nodes[0]
    decision = await router(sim).choose(pathway, greet, inputs("What are your opening hours today?"))
    assert (decision.kind, decision.to_node, decision.degraded) == ("edge", "hours", False)
    [request] = await sim.requests()
    assert request["tool_choice"] == {"type": "function", "function": {"name": "select_path"}}
    stay = await router(sim).choose(pathway, greet, inputs("Hello there"))
    assert stay.kind == "stay"


async def test_invalid_output_is_repaired(sim: Sim) -> None:
    await sim.rules(
        "rules:\n"
        "  - id: broken\n    match: {tool_name: select_path, call_index: 0}\n    respond:\n"
        "      tool_calls: [{name: select_path, arguments: {path_id: e_nope, confidence: 11,"
        " alternatives: [], reason: x}}]\n"
        "  - id: fixed\n    match: {tool_name: select_path, call_index: 1}\n    respond:\n"
        "      tool_calls: [{name: select_path, arguments: {path_id: e_order, confidence: 9,"
        " alternatives: [], reason: wants food}}]\n"
    )
    pathway = restaurant()
    decision = await router(sim).choose(pathway, pathway.nodes[0], inputs("Something"))
    assert (decision.kind, decision.to_node, decision.reason, decision.degraded) == (
        "edge",
        "order",
        "wants food",
        False,
    )
    first, second = await sim.requests()
    assert (first["rule_id"], second["rule_id"]) == ("broken", "fixed")
    assert second["messages"][-1]["content"].startswith("Your previous output was invalid:")


async def test_persistently_invalid_output_degrades_to_the_heuristic(sim: Sim) -> None:
    await sim.rules(
        "rules:\n  - id: bad\n    match: {tool_name: select_path}\n    respond:\n"
        "      tool_calls: [{name: select_path, arguments: '{\"path_id\": '}]\n"
    )
    pathway = restaurant()
    decision = await router(sim).choose(pathway, pathway.nodes[0], inputs("I want to order some food"))
    assert (decision.kind, decision.to_node, decision.degraded) == ("edge", "order", True)
    assert len(await sim.requests()) == 2


async def test_loop_condition_uses_the_node_threshold(sim: Sim) -> None:
    await sim.rules(
        "rules:\n  - id: score\n    match: {tool_name: score_condition}\n    respond:\n"
        "      tool_calls: [{name: score_condition, arguments: {score: 7, met: true, missing: ''}}]\n"
    )
    pathway = restaurant()
    result = await LoopCondition(sim.rig.gateway, clock=FrozenClock(NOW)).score(
        pathway.nodes[0], inputs("I want food")
    )
    assert result is not None
    assert (result.score, result.met, result.model_met, result.message) == (7, False, True, "Likely 7/10")
    [request] = await sim.requests()
    assert "Completion condition: The caller said what they want\n" in request["messages"][0]["content"]


async def test_extraction_coerces_fakellm_values(sim: Sim) -> None:
    variables = [
        {"name": "party_size", "type": "integer", "description": "Party size"},
        {"name": "outdoor", "type": "boolean", "description": "Wants to sit outside"},
    ]
    pathway = graph([start("book", name="Book", extractVars=variables)])
    extractor = Extractor(sim.rig.gateway, clock=FrozenClock(NOW))
    result = await extractor.extract(
        pathway.nodes[0], inputs("Yes, outside please, we are four people"), now=NOW, timezone="UTC"
    )
    assert result is not None
    assert result.values == {"party_size": 4, "outdoor": True}
    await sim.rules(
        "rules:\n  - id: spoken\n    match: {tool_name: extract_variables}\n    respond:\n"
        "      tool_calls: [{name: extract_variables, arguments: {party_size: twenty two, outdoor: nope}}]\n"
    )
    spoken = await extractor.extract(pathway.nodes[0], inputs("whatever"), now=NOW, timezone="UTC")
    assert spoken is not None
    assert spoken.values == {"party_size": 22, "outdoor": False}
