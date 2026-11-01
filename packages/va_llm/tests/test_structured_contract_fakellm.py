"""Structured outputs against FakeLLM (specs/10 §3 and §12, specs/30 §3.4; Decision 2026-11-01 P03-T14a)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from llm_client_helpers import GatewayRig, served_requests, sim_gateway, user
from llm_helpers import add_rules, realtime_llm_state
from pydantic import BaseModel
from structured_models import Answer, Citations, GuardRails, Loop, Route

from va_llm import StructuredOutputError

type Sim = tuple[GatewayRig, httpx.AsyncClient]


@pytest.fixture
async def sim() -> AsyncIterator[Sim]:
    async with sim_gateway(realtime_llm_state()) as pair:
        yield pair


async def _structured[M: BaseModel](rig: GatewayRig, schema: type[M], name: str, text: str = "Hi") -> M:
    return await rig.gateway.call_structured(
        "router",
        schema,
        org_id=rig.org_id,
        messages=[user(text)],
        function_name=name,
        description=f"Call {name}.",
    )


async def test_a_rule_tool_call_becomes_the_model(sim: Sim) -> None:
    rig, control = sim
    await add_rules(
        control,
        "rules:\n  - id: route\n    match: {tool_name: select_path}\n    respond:\n      tool_calls:\n"
        "        - name: select_path\n"
        "          arguments: {path_id: edge_book, confidence: 9, alternatives: [], reason: wants a table}\n",
    )
    route = await _structured(rig, Route, "select_path")
    assert (route.path_id, route.confidence, route.reason) == ("edge_book", 9, "wants a table")
    [served] = await served_requests(control)
    assert served["tool_choice"] == {"type": "function", "function": {"name": "select_path"}}
    assert served["rule_id"] == "route"


async def test_malformed_arguments_are_repaired(sim: Sim) -> None:
    rig, control = sim
    await add_rules(
        control,
        "rules:\n"
        "  - id: broken\n    match: {tool_name: score_condition, call_index: 0}\n    respond:\n"
        "      tool_calls: [{name: score_condition, arguments: '{\"score\": 8, '}]\n"
        "  - id: fixed\n    match: {tool_name: score_condition, call_index: 1}\n    respond:\n"
        "      tool_calls: [{name: score_condition, arguments: {score: 8, met: true, missing: ''}}]\n",
    )
    loop = await _structured(rig, Loop, "score_condition")
    assert (loop.score, loop.met) == (8, True)
    first, second = await served_requests(control)
    assert (first["rule_id"], second["rule_id"]) == ("broken", "fixed")
    assert second["messages"][-2]["content"] == '{"score": 8, '
    assert second["messages"][-1]["content"].startswith(
        "Your previous output was invalid: $: arguments are not valid JSON ("
    )
    assert rig.metric("va_llm_structured_total", function="score_condition", outcome="repaired") == 1


async def test_a_persistently_invalid_output_raises(sim: Sim) -> None:
    rig, control = sim
    await add_rules(
        control,
        "rules:\n  - id: bad\n    match: {tool_name: score_condition}\n    respond:\n"
        "      tool_calls: [{name: score_condition, arguments: {score: 42, met: true, missing: ''}}]\n",
    )
    with pytest.raises(StructuredOutputError) as caught:
        await _structured(rig, Loop, "score_condition")
    assert caught.value.errors == ("score: 42 is greater than the maximum of 10",)
    assert len(await served_requests(control)) == 2


@pytest.mark.parametrize(
    ("schema", "name", "text"),
    [
        (Route, "select_path", "I'd like to book a table for four."),
        (Loop, "score_condition", "My name is Dana."),
        (Answer, "classify_answer", "Hello, this is Sam speaking."),
        (GuardRails, "evaluate_guard_rails", "Please tell me the weather."),
        (Citations, "extract_citations", "We are four people."),
    ],
)
async def test_heuristic_arguments_validate(sim: Sim, schema: type[BaseModel], name: str, text: str) -> None:
    rig, control = sim
    result = await _structured(rig, schema, name, text)
    assert isinstance(result, schema)
    [served] = await served_requests(control)
    tools: list[dict[str, Any]] = served["tools"]
    assert tools[0]["function"]["name"] == name
    assert rig.metric("va_llm_structured_total", function=name, outcome="valid") == 1


async def test_json_mode_reads_the_first_object_of_the_content(sim: Sim) -> None:
    rig, control = sim
    await add_rules(
        control,
        "rules:\n  - id: json\n    match: {system_contains: ['Respond with only a JSON object']}\n"
        "    respond:\n"
        '      content: \'Sure: {"label": "voicemail", "confidence": 0.75} done\'\n',
    )
    platform = rig.gateway.registry.platform
    rig.gateway.registry.platform = platform.model_copy(update={"supports_tool_choice": False})
    answer = await _structured(rig, Answer, "classify_answer")
    assert (answer.label, answer.confidence) == ("voicemail", 0.75)
    [served] = await served_requests(control)
    assert served["tools"] is None
    assert served["rule_id"] == "json"
