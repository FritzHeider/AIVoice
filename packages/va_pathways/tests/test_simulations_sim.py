"""Simulations against FakeLLM through the real gateway (specs/12 §9; Decision P07-T11d): the caller hangs
up, the turn cap, the agent ending the call, grading pass/fail, and every error."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from graph_helpers import node
from pathways_helpers import (
    GREETING,
    PATHWAY,
    TextRig,
    broken_user_turns,
    grade_rule,
    job,
    route_rule,
    size_rule,
    text_rig,
)

from va_engine.engine import PathwayEngine
from va_engine.models import load_graph
from va_engine.navigation import ActiveGraph
from va_pathways import simulations
from va_pathways.grading import SIMULATION_GRADE_PROMPT
from va_pathways.simulations import CALLER_SUFFIX, HANGUP_MARKER

pytestmark = pytest.mark.integration

ORDER_REPLY = "Great, what toppings would you like?"


@pytest.fixture
async def rig() -> AsyncIterator[TextRig]:
    async with text_rig() as running:
        yield running


def _caller_rules(*lines: str) -> str:
    """The caller's n-th request (0-based, per conversation) answers ``lines[n]``."""
    rules = ["rules:"]
    for index, line in enumerate(lines):
        rules.append(
            f"  - id: caller{index}\n    priority: 10\n"
            f"    match: {{system_contains: ['{HANGUP_MARKER}'], call_index: {index}}}\n"
            f"    respond:\n      content: {json.dumps(line)}\n"
        )
    return "\n".join(rules) + "\n"


def _is_caller(request: dict[str, Any]) -> bool:
    first = request["messages"][0]
    return first["role"] == "system" and CALLER_SUFFIX in (first["content"] or "")


def _is_grade(request: dict[str, Any]) -> bool:
    return request.get("tool_choice") == {"type": "function", "function": {"name": "grade"}}


async def test_the_caller_hangs_up_and_the_goal_passes(rig: TextRig) -> None:
    await rig.rules(route_rule("e_order"))
    await rig.rules(size_rule("large"))
    await rig.rules(_caller_rules("I want a large pizza.", f"Pepperoni. Thanks, bye! {HANGUP_MARKER}"))
    await rig.rules(grade_rule("true", "Size captured."))
    result = await rig.simulate(job("2-3", goal='Size "captured"'))
    item: Any = result.item()
    assert {key: item[key] for key in ("simulation_id", "scenario", "run", "goal")} == {
        "simulation_id": "2-3",
        "scenario": "Hungry caller",
        "run": 3,
        "goal": 'Size "captured"',
    }
    assert (item["passed"], item["status"], item["ended_by"], item["turns"]) == (True, "passed", "hangup", 1)
    assert item["transcript"] == [
        {"role": "assistant", "content": GREETING},
        {"role": "user", "content": "I want a large pizza."},
        {"role": "assistant", "content": ORDER_REPLY},
        {"role": "user", "content": "Pepperoni. Thanks, bye!"},
    ]
    assert item["variables"] == {"pizza_size": "large"}
    assert item["grading"] == {"pass": True, "rationale": "Size captured."}
    assert item["actions"] == []
    assert isinstance(item["duration_ms"], int)
    assert "error" not in item

    requests = await rig.requests()
    first, second = [request for request in requests if _is_caller(request)]
    assert first["messages"] == [
        {
            "role": "system",
            "content": f"You are a hungry customer who wants a large pizza.\n\n{CALLER_SUFFIX}",
        },
        {"role": "user", "content": GREETING},
    ]
    assert (first["temperature"], first["max_tokens"]) == (0.7, 256)
    assert [message["role"] for message in second["messages"]] == ["system", "user", "assistant", "user"]
    assert second["messages"][-1]["content"] == ORDER_REPLY
    [grading] = [request for request in requests if _is_grade(request)]
    transcript, variables, prompt = grading["messages"][-3:]
    assert transcript["content"] == (
        f"Conversation:\nAgent: {GREETING}\nCaller: I want a large pizza.\nAgent: {ORDER_REPLY}\n"
        "Caller: Pepperoni. Thanks, bye!"
    )
    assert variables["content"] == 'Final variables: {"pizza_size":"large"}'
    assert prompt["content"] == SIMULATION_GRADE_PROMPT.replace("{goal}", 'Size "captured"')
    # The hang-up line never reached the engine: no request after the second caller turn shows it.
    assert not any(
        "Pepperoni" in json.dumps(request["messages"]) for request in requests if not _is_grade(request)
    )


async def test_a_failing_verdict_fails_the_simulation(rig: TextRig) -> None:
    await rig.rules(route_rule("e_order"))
    await rig.rules(_caller_rules(f"Never mind. {HANGUP_MARKER}"))
    await rig.rules(grade_rule("false", "No order."))
    result = await rig.simulate(job())
    assert (result.status, result.passed, result.ended_by, result.turns, result.error) == (
        "failed",
        False,
        "hangup",
        0,
        None,
    )
    assert result.item()["transcript"] == [
        {"role": "assistant", "content": GREETING},
        {"role": "user", "content": "Never mind."},
    ]


async def test_a_caller_that_never_hangs_up_stops_at_max_turns(rig: TextRig) -> None:
    await rig.rules(route_rule("e_order"))
    await rig.rules(grade_rule("true", "Fine."))
    result = await rig.simulate(job(max_turns=3))
    assert (result.status, result.ended_by, result.turns) == ("passed", "max_turns", 3)
    callers = [request for request in await rig.requests() if _is_caller(request)]
    assert len(callers) == 3
    roles = [line[0] for line in result.transcript]
    assert roles == ["assistant", "user", "assistant", "user", "assistant", "user", "assistant"]


async def test_the_agent_ending_the_call_ends_the_simulation(rig: TextRig) -> None:
    await rig.rules(route_rule("e_bye"))
    await rig.rules(_caller_rules("That's all, thanks."))
    await rig.rules(grade_rule("true", "Polite."))
    result = await rig.simulate(job())
    assert (result.status, result.ended_by, result.turns) == ("passed", "agent_ended", 1)
    assert result.transcript[-1] == ("assistant", "Goodbye!")
    assert result.actions == [{"type": "end_call", "reason": "agent_end_call", "ended_by": "agent"}]
    assert len([request for request in await rig.requests() if _is_caller(request)]) == 1


async def test_an_unavailable_caller_is_an_error_without_grading(rig: TextRig) -> None:
    await rig.rules(
        f"rules:\n  - id: down\n    priority: 10\n    match: {{system_contains: ['{HANGUP_MARKER}']}}\n"
        "    respond:\n      error: {status: 400}\n"
    )
    result = await rig.simulate(job())
    item: Any = result.item()
    assert (item["status"], item["ended_by"], item["error"], item["grading"], item["turns"]) == (
        "error",
        "error",
        "caller_unavailable",
        None,
        0,
    )
    assert item["transcript"] == [{"role": "assistant", "content": GREETING}]
    assert not any(_is_grade(request) for request in await rig.requests())


async def test_a_blank_caller_reply_is_invalid(rig: TextRig) -> None:
    await rig.rules(_caller_rules("   "))
    result = await rig.simulate(job())
    assert (result.status, result.ended_by, result.error) == ("error", "error", "caller_invalid")


async def test_an_unavailable_grader_is_an_error(rig: TextRig) -> None:
    await rig.rules(_caller_rules(HANGUP_MARKER))
    await rig.rules(
        "rules:\n  - id: down\n    match: {tool_name: grade}\n    respond:\n      error: {status: 400}\n"
    )
    result = await rig.simulate(job())
    assert (result.status, result.ended_by, result.error, result.grading) == (
        "error",
        "hangup",
        "grading_unavailable",
        None,
    )
    assert result.item()["transcript"] == [{"role": "assistant", "content": GREETING}]


async def test_engine_failures_and_the_turn_cap_are_errors(
    rig: TextRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await rig.rules(_caller_rules("Hello?", "Hello?"))
    with monkeypatch.context() as patched:
        patched.setattr(PathwayEngine, "handle_user_turn", broken_user_turns)
        failed = await rig.simulate(job())
    assert (failed.status, failed.ended_by, failed.error, failed.turns) == (
        "error",
        "error",
        "engine_error",
        1,
    )

    monkeypatch.setattr(simulations, "CASE_TIMEOUT_S", 0.2)
    await rig.rules(
        "rules:\n  - id: slow\n    match: {tool_name: select_path}\n    respond:\n      delay_ms: 5000\n"
    )
    slow = await rig.simulate(job())
    assert (slow.status, slow.error) == ("error", "turn_timeout")


async def test_a_version_without_a_start_node_is_unavailable(rig: TextRig) -> None:
    graph = load_graph([node("lonely", name="Lonely", text="Hi.")], [], {})
    result = await rig.simulate(job(), ActiveGraph(PATHWAY, 1, graph))
    assert (result.status, result.error, result.transcript) == ("error", "version_unavailable", [])
