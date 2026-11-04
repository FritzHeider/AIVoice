"""The unit-test case runner against FakeLLM through the real gateway (specs/12 §8; Decision P07-T11a):
route, variables, grading, variations, tool mocks, real webhooks, and every case error."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import orjson
import pytest
from pathways_helpers import (
    GREETING,
    TextRig,
    bella_pasta,
    broken_user_turns,
    grade_rule,
    route_rule,
    size_rule,
    spec,
    text_rig,
)
from webhook_receiver import Receiver, Reply, served_receiver

from va_engine.engine import PathwayEngine
from va_pathways import unit_tests
from va_pathways.grading import GRADE_PROMPT

pytestmark = pytest.mark.integration


@pytest.fixture
async def rig() -> AsyncIterator[TextRig]:
    async with text_rig() as running:
        yield running


async def test_route_and_variables_pass_with_case_insensitive_strings(rig: TextRig) -> None:
    await rig.rules(size_rule("LARGE"))
    result = await rig.run(
        spec(expected={"route_to_node_id": "order", "variables": {"pizza_size": "large"}}),
        test_id="PT-1",
    )
    item: Any = result.item()
    assert (item["test_id"], item["name"], item["passed"], item["status"]) == (
        "PT-1",
        "Routes to order",
        True,
        "passed",
    )
    [case] = item["cases"]
    assert case == {
        "input": "I want to order some food",
        "passed": True,
        "route": "order",
        "variables": {"pizza_size": "LARGE"},
        "reply": "Great, what toppings would you like?",
        "grading": None,
        "actions": [],
    }
    assert isinstance(item["duration_ms"], int)
    assert "error" not in item
    assert "variations_short" not in item


async def test_wrong_route_or_variable_fails_the_test(rig: TextRig) -> None:
    await rig.rules(route_rule("e_bye"))
    await rig.rules(size_rule("small"))
    result = await rig.run(spec(expected={"route_to_node_id": "order"}))
    [case] = result.cases
    assert (result.status, result.passed, case.passed, case.route, case.error) == (
        "failed",
        False,
        False,
        "bye",
        None,
    )
    assert case.reply == "Goodbye!"
    assert case.actions == [{"type": "end_call", "reason": "agent_end_call", "ended_by": "agent"}]
    sized = await rig.run(spec(expected={"variables": {"pizza_size": "large", "missing": 1}}))
    assert (sized.status, sized.cases[0].variables) == ("failed", {"pizza_size": "small"})


async def test_grading_goal_pass_and_fail(rig: TextRig) -> None:
    goal = 'Ask about toppings {"strictly"}.'
    await rig.rules(grade_rule("true", "Asked about toppings."))
    passed = await rig.run(spec(expected={"route_to_node_id": "order", "grading_goal": goal}))
    assert passed.status == "passed"
    assert dict(passed.cases[0].item())["grading"] == {"pass": True, "rationale": "Asked about toppings."}
    [grading] = [
        request
        for request in await rig.requests()
        if request.get("tool_choice") == {"type": "function", "function": {"name": "grade"}}
    ]
    assert grading["temperature"] == 0
    transcript, prompt = grading["messages"][-2:]
    assert transcript["content"] == (
        f"Conversation:\nAgent: {GREETING}\nCaller: I want to order some food\n"
        "Agent: Great, what toppings would you like?"
    )
    assert prompt["content"] == GRADE_PROMPT.replace("{goal}", goal)
    await rig.rules(grade_rule("false", "Did not confirm the size."))
    failed = await rig.run(spec(expected={"route_to_node_id": "order", "grading_goal": goal}))
    [case] = failed.cases
    assert (failed.status, case.passed, case.error) == ("failed", False, None)
    assert case.grading is not None
    assert case.grading.item() == {"pass": False, "rationale": "Did not confirm the size."}


async def test_unavailable_grader_is_a_case_error(rig: TextRig) -> None:
    await rig.rules(
        "rules:\n  - id: down\n    match: {tool_name: grade}\n    respond:\n      error: {status: 400}\n"
    )
    result = await rig.run(spec(expected={"route_to_node_id": "order", "grading_goal": "Be nice."}))
    [case] = result.cases
    assert (result.status, case.passed, case.error, case.grading) == (
        "error",
        False,
        "grading_unavailable",
        None,
    )
    assert case.item()["error"] == "grading_unavailable"
    wrong = await rig.run(spec(expected={"route_to_node_id": "bye", "grading_goal": "Be nice."}))
    assert (wrong.status, wrong.cases[0].error) == ("failed", "grading_unavailable")


async def test_variations_run_as_extra_cases(rig: TextRig) -> None:
    await rig.rules(
        "rules:\n  - id: rewrite\n    match: {system_contains: [Rewrite the caller message]}\n    respond:\n"
        '      content: \'Sure: ["Can I order some food", "i want to order some FOOD", 7, "", '
        '"I would like to order food please", "Can I order some food", "Order food now"] done\'\n'
    )
    result = await rig.run(spec(expected={"route_to_node_id": "order"}, simulated_variations=2))
    assert [case.input for case in result.cases] == [
        "I want to order some food",
        "Can I order some food",
        "I would like to order food please",
    ]
    assert (result.status, result.variations_short) == ("passed", 0)
    [rewrite] = [request for request in await rig.requests() if request["rule_id"] == "rewrite"]
    assert rewrite["messages"][0]["content"].startswith(
        "Rewrite the caller message in 2 different natural ways"
    )
    assert rewrite["messages"][1] == {"role": "user", "content": "I want to order some food"}
    short = await rig.run(spec(expected={"route_to_node_id": "order"}, simulated_variations=5))
    assert (len(short.cases), short.variations_short, short.item()["variations_short"]) == (4, 2, 2)


async def test_one_failing_variation_fails_the_test(rig: TextRig) -> None:
    await rig.rules(
        "rules:\n  - id: rewrite\n    match: {system_contains: [Rewrite the caller message]}\n    respond:\n"
        '      content: \'["Can I order some food", "Thanks, goodbye"]\'\n'
    )
    result = await rig.run(spec(expected={"route_to_node_id": "order"}, simulated_variations=2))
    assert [(case.input, case.passed, case.route) for case in result.cases] == [
        ("I want to order some food", True, "order"),
        ("Can I order some food", True, "order"),
        ("Thanks, goodbye", False, "bye"),
    ]
    assert (result.status, result.passed) == ("failed", False)


async def test_unavailable_variations_are_a_test_error(rig: TextRig) -> None:
    await rig.rules(
        "rules:\n  - id: rewrite\n    match: {system_contains: [Rewrite the caller message]}\n    respond:\n"
        "      error: {status: 400}\n"
    )
    result = await rig.run(spec(expected={"route_to_node_id": "order"}, simulated_variations=3))
    assert (len(result.cases), result.status, result.error, result.variations_short) == (
        1,
        "error",
        "variations_unavailable",
        0,
    )
    assert result.item()["error"] == "variations_unavailable"
    failing = await rig.run(spec(expected={"route_to_node_id": "bye"}, simulated_variations=3))
    assert failing.status == "failed"


async def test_mocked_webhook_never_reaches_the_endpoint() -> None:
    hooks = Receiver(Reply(200, headers={"content-type": "application/json"}, body=b'{"eta": 25}'))
    async with served_receiver(hooks) as port, text_rig() as rig:
        test = spec(
            "What is my delivery status?",
            expected={
                "route_to_node_id": "hook",
                "variables": {"eta": 40},
                "mock_tools": {"hook": {"status": 200, "body": {"eta": 40}}},
            },
        )
        mocked = await rig.run(test, bella_pasta(port))
        assert hooks.requests == []
        [case] = mocked.cases
        assert (mocked.status, case.reply) == (
            "passed",
            "Let me check that for you. It arrives in 40 minutes.",
        )
        real = await rig.run(
            spec("What is my delivery status?", expected={"variables": {"eta": 25}}), bella_pasta(port)
        )
        [received] = hooks.requests
        assert orjson.loads(received.body) == {"caller": "+14155550123"}
        assert real.status == "passed"


async def test_turn_timeout_is_a_case_error(rig: TextRig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(unit_tests, "CASE_TIMEOUT_S", 0.2)
    await rig.rules(
        "rules:\n  - id: slow\n    match: {tool_name: select_path}\n    respond:\n      delay_ms: 5000\n"
    )
    result = await rig.run(spec(expected={"route_to_node_id": "greet"}))
    [case] = result.cases
    assert (result.status, case.passed, case.error, case.route) == ("error", False, "turn_timeout", "greet")


async def test_engine_failure_is_a_case_error(rig: TextRig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(PathwayEngine, "handle_user_turn", broken_user_turns)
    result = await rig.run(spec())
    assert (result.status, result.cases[0].error) == ("error", "engine_error")


async def test_bad_history_and_missing_node_fail_without_running(rig: TextRig) -> None:
    bad = spec(history=[{"role": "tool", "content": "x"}], simulated_variations=0)
    missing = spec().model_copy(update={"node_id": "gone"})
    results = [await rig.run(bad), await rig.run(missing)]
    assert [(r.status, r.cases[0].error, r.cases[0].route) for r in results] == [
        ("error", "invalid_history", None),
        ("error", "unknown_node", None),
    ]
    assert await rig.requests() == []
