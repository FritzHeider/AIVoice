"""A finished pathway conversation sits on no reference cycle (specs/07 §9 Decisions P08-FIX-3, P08-FIX-24).

The engine runs on the media loop: a conversation that is only freed by the cyclic collector costs every
live call a collector pass. These tests run whole conversations (routing, extraction, speculation, a node
tool round, per-turn data) and then ask whether any engine part lies on a cycle of its own.
"""

from __future__ import annotations

import json
from typing import Final

from engine_helpers import Rig, engine_rig, route
from gc_cycles import cycles_through, live_cycles_through, unreachable_after
from graph_helpers import edge, graph, node, start
from task_helpers import RecordingExecutor
from turn_data_fakes import FakeTurnData

from va_engine.models import PathwayGraph
from va_engine.pathway_brain import PathwayBrain
from va_engine.system_tools import SystemToolFacts
from va_llm.models import ToolCall, ToolDef

ENGINE_PARTS: Final = (
    "PathwayEngine",
    "PathwayBrain",
    "Navigator",
    "GatewayDialogue",
    "Speculation",
    "NodeTools",
    "EnginePorts",
)
LOOKUP: Final = ToolDef(
    name="lookup",
    description="Look up opening hours",
    parameters={"type": "object", "properties": {"city": {"type": "string"}}},
)


def _booking() -> PathwayGraph:
    """A greeting that extracts the caller's name, then a reply node."""
    return graph(
        [
            start(
                "a",
                text="Hi.",
                extractVars=[{"name": "first_name", "type": "string", "description": "Name"}],
            ),
            node("b", name="Book", prompt="Help them book."),
        ],
        [edge("e", "a", "b", "wants to book")],
    )


async def _booked() -> tuple[Rig, PathwayBrain]:
    """A closed conversation: opener, one routed caller turn with an extraction and a streamed reply."""
    rig = engine_rig(_booking())
    rig.port.script("select_path", route("e")).script("extract_variables", {"first_name": "Mike"})
    rig.source.reply("Sure, ", "when?").reply("Sure, ", "when?")
    brain = PathwayBrain(rig.engine, clock=rig.clock)
    await rig.start()
    await rig.say("I am Mike and I want to book")
    await brain.aclose()
    return rig, brain


async def test_a_closed_conversation_leaves_no_engine_part_on_a_cycle() -> None:
    rig, brain = await _booked()

    assert rig.state.variables["first_name"] == "Mike"  # the extraction port did its work
    assert rig.state.current_node_id == "b"
    assert live_cycles_through(brain, ENGINE_PARTS) == {}


async def test_a_dropped_conversation_is_freed_without_the_collector() -> None:
    with unreachable_after() as garbage:
        rig, brain = await _booked()
        assert rig.state.current_node_id == "b"
        del rig, brain

    assert cycles_through(garbage, ENGINE_PARTS) == {}


async def test_a_conversation_with_node_tools_and_turn_data_leaves_no_cycle() -> None:
    tools = RecordingExecutor(tools=(LOOKUP,), names=frozenset({"lookup"}), refs={"TL-1": "lookup"})
    turn_data = FakeTurnData(lines=("Their plan is gold.",)).then(
        {"plan": "platinum"}, "Their plan is platinum."
    )
    rig = engine_rig(
        graph([start("main", prompt="Answer questions.", tools=["TL-1"])], []),
        tools=tools,
        system=SystemToolFacts(runner="pathway", channel="voice"),
        fillers=False,
        turn_data=turn_data,
    )
    asked = ToolCall(id="c1", name="lookup", arguments_json=json.dumps({"city": "Paris"}))
    rig.source.reply("Hello.").reply("speculative")
    rig.source.reply("Checking. ", calls=[asked]).reply("We open at nine.")
    brain = PathwayBrain(rig.engine, clock=rig.clock)
    await rig.start()

    await rig.say("when do you open")
    await brain.aclose()

    assert [call.name for call in tools.calls] == ["lookup"]  # the tool round ran
    assert turn_data.applied == [{"plan": "platinum"}]  # and the turn's refresh was committed
    assert live_cycles_through(brain, ENGINE_PARTS) == {}
