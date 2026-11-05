"""A pathway engine running real node tools through :class:`PathwayCallExecutor` (specs/13 §4; Decision
P08-T02c2): two reply nodes hold different inline tools of one name, and each node speaks and runs its own;
the tool usage note quotes the call's tool speech, also when a graph the call switched to brought it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from engine_helpers import Rig, engine_rig, route
from graph_helpers import edge, graph, node, start
from navigation_helpers import P2, MemoryLoader, active
from tools_helpers import API, RecordedEvents, plain_executor

from va_engine.models.nodes import PathwayVersionRef
from va_engine.navigation import ActiveGraph, PathwayLoader
from va_llm.models import ToolCall
from va_tools.pathway_executor import PathwayCallExecutor, graph_tool_refs
from va_tools.runner import ToolRunner


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


def _inline(path: str, speech: str | None) -> dict[str, Any]:
    tool = {
        "name": "lookup",
        "description": f"Looks things up at {path}.",
        "url": f"{API}{path}",
        "response": {"answer": "$.answer"},
    }
    return tool if speech is None else {**tool, "speech": speech}


def _lookup(call_id: str) -> ToolCall:
    return ToolCall(id=call_id, name="lookup", arguments_json="{}")


def _tool_records(rig: Rig) -> list[Any]:
    return [json.loads(record.content)["data"] for record in rig.state.history if record.role == "tool"]


async def test_each_node_speaks_and_runs_its_own_tool_of_one_name(api: respx.MockRouter) -> None:
    hours = api.post("/hours").respond(200, json={"answer": "nine to five"})
    orders = api.post("/orders").respond(200, json={"answer": "shipped"})
    pathway = graph(
        [
            start("a", prompt="Help.", tools=[_inline("/hours", "Checking the hours.")]),
            node("b", prompt="More.", tools=[_inline("/orders", "Checking the order.")]),
        ],
        [edge("e1", "a", "b", "Caller asks about an order")],
    )
    events = RecordedEvents()
    async with plain_executor() as (http, clock):
        tools = PathwayCallExecutor(runner=ToolRunner(http, clock=clock), events=events)
        rig = engine_rig(pathway, tools=tools, speculative=False)
        rig.source.reply("", calls=[_lookup("c1")]).reply("We open at nine.")
        rig.source.reply("", calls=[_lookup("c2")]).reply("It has shipped.")

        opened = await rig.start()
        rig.port.script("select_path", route("e1", 9))
        ordered = await rig.say("Where is my order?")

    assert [action[:2] for action in opened if action[0] == "say"] == [("say", "Checking the hours.")]
    assert [action[:2] for action in ordered if action[0] == "say"] == [("say", "Checking the order.")]
    assert (hours.call_count, orders.call_count) == (1, 1)
    assert _tool_records(rig) == [{"answer": "nine to five"}, {"answer": "shipped"}]
    assert rig.state.variables["answer"] == "shipped"
    assert [data["tool_name"] for data in events.tools] == ["lookup", "lookup"]
    descriptions = [[tool.description for tool in request.tools] for request in rig.source.requests]
    assert descriptions == [
        ["Looks things up at /hours."],
        ["Looks things up at /hours."],
        ["Looks things up at /orders."],
        ["Looks things up at /orders."],
    ]


class _ToolAddingLoader:
    """A pathway loader that makes every loaded graph's tools known to the call's executor, as the media
    worker's loader does."""

    def __init__(self, inner: PathwayLoader, tools: PathwayCallExecutor) -> None:
        self._inner = inner
        self._tools = tools

    async def load(self, pathway_id: str, version: PathwayVersionRef) -> ActiveGraph:
        loaded = await self._inner.load(pathway_id, version)
        self._tools.add((), graph_tool_refs(loaded.graph))
        return loaded


def _heard(rig: Rig) -> list[str]:
    """What each dialogue request's tool usage note says the caller will hear."""
    notes = [
        "\n".join(str(message.content) for message in request.messages) for request in rig.source.requests
    ]
    return [re.findall(r'the caller will hear "([^"]*)"', note)[0] for note in notes]


async def test_the_usage_note_quotes_the_tool_speech_a_later_graph_brought() -> None:
    other = graph(
        [
            start("billing", prompt="Ask about the bill.", tools=[_inline("/orders", "Checking the order.")]),
            node("refund", prompt="Refund.", tools=[_inline("/refunds", "Refunding.")]),
        ],
        [edge("e_refund", "billing", "refund", "Caller wants a refund")],
    )
    pathway = graph(
        [
            start("a", prompt="Help.", tools=[_inline("/hours", None)]),
            node("hop", node_type="Transfer Pathway", transferPathway={"pathwayId": str(P2), "version": 3}),
        ],
        [edge("e_hop", "a", "hop", "Caller has a billing question")],
    )
    async with plain_executor() as (http, clock):
        tools = PathwayCallExecutor(runner=ToolRunner(http, clock=clock))
        tools.add((), graph_tool_refs(pathway))
        loader = _ToolAddingLoader(MemoryLoader({(str(P2), 3): active(other, P2, 3)}), tools)
        rig = engine_rig(pathway, tools=tools, loader=loader, speculative=False)
        rig.source.reply("How can I help?").reply("Which bill?").reply("Refunding it.")

        await rig.start()
        rig.port.script("select_path", route("e_hop", 9))
        await rig.say("It is about my bill.")
        rig.port.script("select_path", route("e_refund", 9))
        await rig.say("I want my money back.")

    assert rig.state.current_node_id == "refund"
    # No tool of the pinned graph has a speech; the first one of the graph switched to stays the call's.
    assert _heard(rig) == ["One moment.", "Checking the order.", "Checking the order."]
