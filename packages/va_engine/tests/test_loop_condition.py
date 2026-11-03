"""Loop-condition scoring (specs/11 §5.4; specs/10 §9.2; specs/11 §5.5 Decision P07-T04 rules 4 and 6)."""

from __future__ import annotations

from typing import Any

import pytest
from decision_helpers import CALL_ID, ORG_ID, ScriptedPort, inputs, invalid, unavailable
from graph_helpers import graph, start
from service_helpers import json_log_stream, log_events

from va_engine.loop_condition import ConditionResult, LoopCondition, not_met_suffix, render_condition_prompt
from va_engine.models.nodes import PathwayNode

CONDITION = "The caller gave their first name"


def condition_node(**data: Any) -> PathwayNode:
    body = {"condition": CONDITION, **data}
    return graph([start("ask", name="Ask Name", **body)]).nodes[0]


async def score(
    node: PathwayNode, *script: dict[str, Any] | Exception
) -> tuple[ConditionResult | None, ScriptedPort]:
    port = ScriptedPort(list(script))
    result = await LoopCondition(port, clock=port.clock).score(
        node, inputs("It's Mike.", ("assistant", "Name?"))
    )
    return result, port


def output(score_: int, *, met: bool = False, missing: str = "") -> dict[str, Any]:
    return {"score": score_, "met": met, "missing": missing}


@pytest.mark.parametrize(("value", "met"), [(5, False), (6, True), (10, True)])
async def test_default_threshold_is_six(value: int, met: bool) -> None:
    result, port = await score(condition_node(), output(value))
    assert result is not None
    assert (result.score, result.met, result.threshold, result.message) == (
        value,
        met,
        6,
        f"Likely {value}/10",
    )
    assert result.event_data() == {"score": value, "met": met}
    [request] = port.requests
    assert (request.call_type, request.function_name, request.org_id, request.call_id) == (
        "loop_condition",
        "score_condition",
        ORG_ID,
        CALL_ID,
    )
    assert request.user == 'Agent: Name?\nLatest caller message: "It\'s Mike."'


async def test_node_threshold_decides_not_the_model_flag() -> None:
    result, _ = await score(condition_node(conditionThreshold=8), output(7, met=True, missing="surname"))
    assert result is not None
    assert (result.met, result.model_met, result.missing, result.ms) == (False, True, "surname", 250)


async def test_no_condition_makes_no_call() -> None:
    result, port = await score(condition_node(condition="  "))
    assert result is None
    assert port.requests == []


@pytest.mark.parametrize("failure", [unavailable(), invalid("score_condition")])
async def test_failure_counts_as_not_met(failure: Exception) -> None:
    stream = json_log_stream()
    result, _ = await score(condition_node(), failure)
    assert result is not None
    assert (result.score, result.met, result.degraded, result.message) == (
        None,
        False,
        True,
        "Condition check unavailable",
    )
    assert result.event_data() == {"score": None, "met": False}
    [event] = [e for e in log_events(stream) if e["event"] == "engine.loop_condition_degraded"]
    assert (event["node_id"], event["reason"]) == ("ask", type(failure).__name__)


def test_prompt_without_examples() -> None:
    assert render_condition_prompt(condition_node()) == (
        "You check whether a step of a phone conversation is complete.\n\n"
        f"Completion condition: {CONDITION}\n\n"
        "Read the conversation and decide how likely it is that the condition has been fully satisfied, "
        "from 0 (certainly not) to 10 (certainly yes). If anything the condition requires is still missing, "
        "say what is missing.\nCall score_condition exactly once."
    )


def test_prompt_with_examples() -> None:
    examples = [
        {
            "conversation": [
                {"role": "assistant", "content": "Your name?"},
                {"role": "user", "content": "It's\nMike."},
                {"role": "system", "content": "ignored"},
            ],
            "conditionMet": True,
            "explanation": "Name given",
        },
        {"conversation": [{"role": "user", "content": "Why?"}], "explanation": "No name"},
    ]
    prompt = render_condition_prompt(condition_node(conditionExamples=examples))
    assert prompt.endswith(
        "say what is missing.\nExamples:\n"
        "- Conversation: Agent: Your name? / Caller: It's Mike. → satisfied: true (Name given)\n"
        "- Conversation: Caller: Why? → satisfied: false (No name)\n\n"
        "Call score_condition exactly once."
    )


def test_not_met_suffix() -> None:
    assert not_met_suffix(CONDITION) == (
        "The following condition is not yet satisfied; keep working toward it: "
        '"The caller gave their first name"'
    )
