"""Tool round planning (specs/10 §7, specs/13 §5; Decision P05-T09c)."""

from __future__ import annotations

import orjson

from va_engine.system_tools import SystemToolFacts
from va_engine.task_tools import (
    ENDING,
    SKIPPED,
    TOO_MANY_TOOL_CALLS,
    TRANSFERRING,
    plan_round,
    sent_digits,
    tool_result,
)
from va_llm.models import ToolCall

FACTS = SystemToolFacts(runner="task", transfer_list={"sales": "+14155550111"})


def _call(index: int, name: str, **arguments: object) -> ToolCall:
    return ToolCall(id=f"c{index}", name=name, arguments_json=orjson.dumps(arguments).decode())


def _never(name: str) -> bool:
    del name
    return False


def test_classification_in_call_order() -> None:
    calls = [
        _call(0, "lookup"),
        _call(1, "press_button", digits="9"),
        _call(2, "press_button", digits="no"),
        _call(3, "transfer_call", label="sales"),
        _call(4, "end_call", reason="x"),
    ]
    plan = plan_round(calls, FACTS, handles=lambda name: name == "lookup", budget=100)
    assert [item.kind for item in plan.calls] == ["execute", "press", "answer", "transfer", "answer"]
    assert plan.calls[1].answer == sent_digits("9")
    assert plan.calls[3].answer == TRANSFERRING
    assert plan.calls[3].target == "+14155550111"
    assert plan.calls[4].answer == SKIPPED
    assert (plan.invalid, plan.used) == (True, 4)
    assert plan.terminal is plan.calls[3]
    assert plan.executed == (plan.calls[0],)


def test_overflow_past_the_round_cap_and_the_budget() -> None:
    calls = [_call(index, "press_button", digits="1") for index in range(6)]
    plan = plan_round(calls, FACTS, handles=_never, budget=100)
    assert [item.answer for item in plan.calls[4:]] == [TOO_MANY_TOOL_CALLS] * 2
    assert plan.used == 4
    small = plan_round(calls[:3], FACTS, handles=_never, budget=1)
    assert [item.kind for item in small.calls] == ["press", "answer", "answer"]
    assert (small.used, small.invalid) == (1, False)
    assert plan_round(calls[:1], FACTS, handles=_never, budget=-3).used == 0


def test_end_call_and_unknown_tools() -> None:
    plan = plan_round(
        [_call(0, "end_call", reason="done"), _call(1, "mystery")], FACTS, handles=_never, budget=9
    )
    assert plan.calls[0].answer == ENDING
    assert plan.calls[0].end is not None
    assert plan.calls[1].answer == SKIPPED
    unknown = plan_round([_call(0, "mystery")], FACTS, handles=_never, budget=9)
    assert orjson.loads(unknown.calls[0].answer or "") == {
        "error": "invalid_arguments",
        "detail": "unknown_tool",
    }
    assert unknown.terminal is None
    assert tool_result({"a": 1}) == '{"a":1}'
