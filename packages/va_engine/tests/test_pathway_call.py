"""Pathway call helpers of the media runtime: the start node's opener and the engine snapshot key and
TTL (specs/11 §10 Decision P07-T12b)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from dialogue_helpers import NOW
from engine_helpers import PATHWAY_ID, engine_rig
from graph_helpers import graph, node, start
from task_helpers import task_context

from va_engine.models import PathwayGraph
from va_engine.navigation import ActiveGraph
from va_engine.pathway_call import (
    ENGINE_STATE_TTL_S,
    engine_state_key,
    opener_text,
    pathway_facts,
    start_state,
)

TEMPLATES = [
    "Hello from Bella Pasta.",
    "  Hi {{first_name}}, welcome to {{restaurant_name}}!  ",
    "Calling {{to}} about order {{order.id}}.",
    "Broken {{ template",
]


def _context(**config: Any) -> Any:
    config.setdefault("wait_for_greeting", False)
    return task_context(
        runner_type="pathway",
        pathway_id=str(PATHWAY_ID),
        pathway_version=2,
        request_data={"restaurant_name": "Bella", "order": {"id": 42}},
        variables={"first_name": "Mike"},
        **config,
    )


@pytest.mark.parametrize("text", TEMPLATES)
async def test_the_opener_is_the_engines_first_line(text: str) -> None:
    pathway = graph([start("a", text=text), node("b", text="B.")])
    active = ActiveGraph(PATHWAY_ID, 2, pathway)
    context = _context()
    rig = engine_rig(pathway, state=start_state(active, context), facts=pathway_facts(context, now=NOW))
    [first, *_] = await rig.start()
    assert first[0] == "say"
    assert opener_text(active, context, now=NOW) == first[1]


def _opener(pathway: PathwayGraph, **config: Any) -> str | None:
    return opener_text(ActiveGraph(PATHWAY_ID, 2, pathway), _context(**config), now=NOW)


def test_the_opener_follows_start_node_id() -> None:
    pathway = graph([start("a", text="A."), node("b", text="Hi {{first_name}}.")])
    assert _opener(pathway, start_node_id="b") == "Hi Mike."


@pytest.mark.parametrize(
    ("nodes", "config"),
    [
        ([start("a", prompt="Greet the caller.")], {}),
        ([start("a", text="   ")], {}),
        ([start("a", text="{{nothing}}")], {}),
        ([start("a", node_type="Wait for Response", text="Hi.")], {}),
        ([start("a", text="Hi.", isGlobal=True, globalLabel="Help")], {}),
        ([start("a", text="Hi.")], {"wait_for_greeting": True}),
    ],
)
def test_no_opener(nodes: list[dict[str, Any]], config: dict[str, Any]) -> None:
    assert _opener(graph(nodes), **config) is None


def test_engine_state_key() -> None:
    call_id = uuid.UUID("0193c5d2-0000-7000-8000-000000000001")
    assert engine_state_key(call_id) == "call:{0193c5d2-0000-7000-8000-000000000001}:engine"
    assert ENGINE_STATE_TTL_S == 7200
