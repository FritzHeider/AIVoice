"""TaskBrain with per-turn dynamic data (specs/13 §7 Decision P08-T03b)."""

from __future__ import annotations

import asyncio
from typing import Any

import orjson
import pytest
from dialogue_helpers import NOW, FakeSource
from service_helpers import json_log_stream, log_events
from task_helpers import RecordingExecutor, drain, scripted_brain, task_context, user_turn
from turn_data_fakes import FakeTurnData

from va_common.time import FrozenClock
from va_engine.task_runner import TaskBrain
from va_engine.tool_executor import ToolExecutor
from va_llm.models import ToolCall, ToolDef

STATIC = "The customer's name is Ana."
GOLD = "Their plan is gold."
PLATINUM = "Their plan is platinum."
LOOKUP = ToolDef(name="lookup_order", parameters={"type": "object"})


def _brain(
    source: FakeSource,
    port: FakeTurnData | None,
    *,
    states: list[dict[str, Any]] | None = None,
    executor: ToolExecutor | None = None,
) -> TaskBrain:
    context = task_context(
        variables={"plan": "gold"}, task="Confirm the {{plan}} plan.", first_sentence="Hi, this is Maya."
    ).model_copy(update={"context_sections": [STATIC], "turn_sections": {1: [GOLD]}})
    return scripted_brain(
        context,
        source,
        clock=FrozenClock(NOW),
        executor=executor,
        fillers=False,
        states=states,
        turn_data=port,
    )


def _system(source: FakeSource, index: int = -1) -> str:
    return source.requests[index].messages[0].content or ""


async def test_the_turn_s_request_carries_the_refreshed_line_and_variable() -> None:
    source = FakeSource().reply("Sure.").reply("Of course.")
    port = FakeTurnData(lines=(GOLD,)).then({"plan": "platinum", "seats": 4}, PLATINUM, "They have 4 seats.")
    states: list[dict[str, Any]] = []
    brain = _brain(source, port, states=states)

    await drain(brain.on_user_turn(user_turn(1, "hello")))

    system = _system(source)
    assert "Confirm the platinum plan." in system
    assert "Known information:\n- plan: platinum\n- seats: 4" in system
    assert system.endswith(f"\n\n## Context\n{STATIC}\n{PLATINUM}\nThey have 4 seats.")
    assert port.scopes == [{"plan": "gold"}]
    assert brain.state.variables == {"plan": "platinum", "seats": 4}
    assert states == [{"plan": "platinum", "seats": 4}]
    assert port.applied == [{"plan": "platinum", "seats": 4}]  # reported with the merge, once

    await drain(brain.on_user_turn(user_turn(2, "and now?")))

    assert port.scopes == [{"plan": "gold"}, {"plan": "platinum", "seats": 4}]
    assert _system(source).endswith(f"\n\n## Context\n{STATIC}\n{PLATINUM}\nThey have 4 seats.")
    assert len(states) == 1  # the second refresh set nothing new
    assert port.applied == [{"plan": "platinum", "seats": 4}, {}]  # and was still applied and reported


async def test_a_turn_refreshes_once_whatever_its_tool_rounds() -> None:
    call = ToolCall(id="call_1", name="lookup_order", arguments_json=orjson.dumps({}).decode())
    source = FakeSource().reply("One moment.", calls=[call]).reply("It ships tomorrow.")
    port = FakeTurnData(lines=(GOLD,)).then({"plan": "platinum"}, PLATINUM)
    executor = RecordingExecutor(tools=(LOOKUP,), names=frozenset({"lookup_order"}))
    brain = _brain(source, port, executor=executor)

    spoken = await drain(brain.on_user_turn(user_turn(1, "where is my order")))

    assert [item[1] for item in spoken] == ["One moment.", "It ships tomorrow."]
    assert len(source.requests) == 2
    assert len(port.scopes) == 1
    assert all(PLATINUM in _system(source, index) for index in (0, 1))


@pytest.mark.parametrize("greeted", [False, True], ids=["opener", "greeting"])
async def test_the_opener_does_not_refresh(greeted: bool) -> None:
    source = FakeSource()
    port = FakeTurnData(lines=(GOLD,)).then({"plan": "platinum"}, PLATINUM)
    brain = _brain(source, port)

    opener = await drain(brain.start(user_turn(1, "hello?") if greeted else None))

    assert opener == [("static", "Hi, this is Maya.", True)]
    assert port.scopes == []
    assert brain.state.variables == {"plan": "gold"}


async def test_without_a_port_the_context_s_turn_lines_are_used() -> None:
    source = FakeSource().reply("Sure.")
    brain = _brain(source, None)

    await drain(brain.on_user_turn(user_turn(1, "hello")))

    assert _system(source).endswith(f"\n\n## Context\n{STATIC}\n{GOLD}")


async def test_a_port_that_raises_lets_the_reply_go_on_with_the_earlier_values() -> None:
    stream = json_log_stream()
    source = FakeSource().reply("Sure.")
    port = FakeTurnData(lines=(GOLD,)).failing(RuntimeError("boom"))
    states: list[dict[str, Any]] = []
    brain = _brain(source, port, states=states)

    spoken = await drain(brain.on_user_turn(user_turn(1, "hello")))

    assert spoken == [("text", "Sure.")]
    assert _system(source).endswith(f"\n\n## Context\n{STATIC}\n{GOLD}")
    assert "- plan: gold" in _system(source)
    assert states == []
    assert [line["event"] for line in log_events(stream)].count("engine.turn_data_failed") == 1


async def test_a_turn_cut_during_the_refresh_merges_nothing_and_the_next_turn_refreshes() -> None:
    source = FakeSource().reply("Sure.")
    hold = asyncio.Event()
    port = FakeTurnData(lines=(GOLD,), hold=hold).then({"plan": "platinum"}, PLATINUM)
    states: list[dict[str, Any]] = []
    brain = _brain(source, port, states=states)
    cut = asyncio.create_task(drain(brain.on_user_turn(user_turn(1, "hello"))))
    async with asyncio.timeout(5):
        await port.started.wait()

    cut.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cut

    assert port.cancelled == 1
    assert (brain.state.variables, states, source.requests) == ({"plan": "gold"}, [], [])
    assert (port.sections(), port.applied) == ((GOLD,), [])

    hold.set()
    spoken = await drain(brain.on_user_turn(user_turn(2, "hello there", supersedes=1)))

    assert spoken == [("text", "Sure.")]
    assert port.scopes == [{"plan": "gold"}, {"plan": "gold"}]
    assert brain.state.variables == {"plan": "platinum"}
    assert port.applied == [{"plan": "platinum"}]
    assert _system(source).endswith(f"\n\n## Context\n{STATIC}\n{PLATINUM}")
    assert [message.content for message in source.requests[0].messages if message.role == "user"] == [
        "hello there"
    ]
