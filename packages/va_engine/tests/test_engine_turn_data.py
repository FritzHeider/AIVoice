"""PathwayEngine with per-turn dynamic data (specs/13 §7 Decision P08-T03b)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from engine_helpers import PATHWAY_ID, KeyedPort, Rig, drive, engine_rig, route, user
from graph_helpers import edge, graph, node, start
from service_helpers import json_log_stream, log_events
from turn_data_fakes import FakeTurnData

from va_engine.engine import new_pathway_state
from va_engine.models import PathwayGraph
from va_engine.navigation import ActiveGraph

GOLD = "Their plan is gold."
PLATINUM = "Their plan is platinum."


def _pathway(**ask: Any) -> PathwayGraph:
    return graph(
        [
            start("ask", prompt="Help with the {{plan}} plan.", **ask),
            node("billing", text="Your plan is {{plan}}."),
        ],
        [edge("e_bill", "ask", "billing", "Asks about billing")],
    )


def _rig(pathway: PathwayGraph, turn_data: FakeTurnData, *, port: KeyedPort | None = None) -> Rig:
    """An engine on ``pathway`` whose call already knows ``plan`` = gold."""
    state = new_pathway_state(ActiveGraph(PATHWAY_ID, 1, pathway))
    state.variables["plan"] = "gold"
    return engine_rig(pathway, state=state, port=port, turn_data=turn_data)


async def _asked(turn_data: FakeTurnData, *replies: str) -> tuple[Rig, list[tuple[Any, ...]]]:
    """After the opener the caller asks for the plan at the reply node; ``replies`` script the dialogue
    source for that turn (speculative request first)."""
    rig = _rig(_pathway(), turn_data)
    rig.source.reply("Hello.")
    for reply in replies:
        rig.source.reply(reply)
    await rig.start()
    return rig, await rig.say("which plan am I on")


def _system(rig: Rig, index: int = -1) -> str:
    return rig.source.requests[index].messages[0].content or ""


async def test_the_refresh_runs_beside_the_decision_and_the_node_runs_after_both() -> None:
    gate = asyncio.Event()
    port = KeyedPort(gates={"select_path": gate})
    turn_data = FakeTurnData(lines=(GOLD,)).then({"plan": "platinum"}, PLATINUM)
    rig = _rig(_pathway(), turn_data, port=port)
    rig.source.reply("Hello.").reply("speculative").reply("You are on platinum.")
    await rig.start()

    running = asyncio.create_task(rig.say("which plan am I on"))
    async with asyncio.timeout(5):
        await port.started.wait()
        await turn_data.started.wait()
    await asyncio.sleep(0)

    assert turn_data.scopes == [{"plan": "gold"}]
    assert rig.state.variables == {"plan": "gold"}  # merged only once the decision is in as well
    assert not running.done()
    assert "gold" in port.calls[0].user  # the decision sees the earlier value

    gate.set()
    actions = await running

    assert actions == [("stream", "You are on platinum.", "t1.1", None)]
    assert rig.state.variables == {"plan": "platinum"}
    speculative, fresh = rig.source.streams[-2:]
    assert speculative.request.speculative
    assert speculative.discarded
    assert not fresh.request.speculative
    system = _system(rig)
    assert "Help with the platinum plan." in system
    assert "- plan: platinum" in system
    assert system.endswith(f"\n\n## Context\n{PLATINUM}")
    assert GOLD in (speculative.request.messages[0].content or "")


async def test_a_node_the_turn_routes_to_renders_the_refreshed_value() -> None:
    turn_data = FakeTurnData(lines=(GOLD,)).then({"plan": "platinum"}, PLATINUM)
    rig = _rig(_pathway(), turn_data)
    rig.source.reply("Hello.").reply("speculative")
    rig.port.script("select_path", route("e_bill", 9))
    await rig.start()

    actions = await rig.say("what do I pay for")

    assert actions == [("say", "Your plan is platinum.", "t1.1", None)]
    assert rig.state.current_node_id == "billing"


async def test_an_unchanged_refresh_keeps_the_speculative_reply() -> None:
    turn_data = FakeTurnData(lines=(GOLD,)).then({"plan": "gold"}, GOLD)

    rig, actions = await _asked(turn_data, "You are on gold.")

    assert actions == [("stream", "You are on gold.", "t1.1", None)]
    assert len(turn_data.scopes) == 1
    assert len(rig.source.streams) == 2
    assert rig.source.streams[-1].committed


async def test_a_refresh_that_only_changed_a_line_discards_the_speculative_reply() -> None:
    turn_data = FakeTurnData(lines=(GOLD,)).then({"plan": "gold"}, "Their plan is gold until May.")

    rig, actions = await _asked(turn_data, "speculative", "Gold, until May.")

    assert actions == [("stream", "Gold, until May.", "t1.1", None)]
    speculative, fresh = rig.source.streams[-2:]
    assert speculative.discarded
    assert not fresh.request.speculative
    assert _system(rig).endswith("\n\n## Context\nTheir plan is gold until May.")


async def test_only_a_caller_turn_s_first_pass_refreshes() -> None:
    pathway = graph(
        [
            start("a", text="Hello."),
            node("b", text="One moment.", modelOptions={"skipUserResponse": True}),
            node("c", text="Your plan is {{plan}}."),
        ],
        [edge("e_ab", "a", "b", "Continue"), edge("e_bc", "b", "c", "Continue")],
        {"maxTotalTurns": 1},
    )
    turn_data = FakeTurnData(lines=(GOLD,)).then({"plan": "platinum"}, PLATINUM).then({"plan": "x"}, "x")
    rig = _rig(pathway, turn_data)
    rig.port.script("select_path", route("e_ab", 9), route("e_bc", 9))

    assert await rig.start() == [("say", "Hello.", "t0.1", None)]
    assert turn_data.scopes == []

    actions = await rig.say("go on")  # two passes: a → b, then the chained b → c

    assert [action[:2] for action in actions] == [("say", "One moment."), ("say", "Your plan is platinum.")]
    assert rig.port.names() == ["select_path", "select_path"]
    assert len(turn_data.scopes) == 1

    ended = await rig.say("again")  # over maxTotalTurns: nothing is decided, nothing is refreshed

    assert ended[-1][0] == "end"
    assert len(turn_data.scopes) == 1
    assert rig.state.variables == {"plan": "platinum"}


async def test_a_wait_timeout_does_not_refresh() -> None:
    pathway = graph(
        [
            start(
                "w",
                node_type="Wait for Response",
                waitForResponse={"waitSeconds": 5, "onTimeout": "move_to_node", "timeoutNodeId": "next"},
            ),
            node("next", text="Your plan is {{plan}}."),
        ]
    )
    turn_data = FakeTurnData(lines=(GOLD,)).then({"plan": "platinum"}, PLATINUM)
    rig = _rig(pathway, turn_data)
    await rig.start()

    assert await rig.timeout() == [("say", "Your plan is gold.", "t0.1", None)]
    assert turn_data.scopes == []


async def test_the_turn_s_extraction_wins_a_name_the_refresh_also_set() -> None:
    extract = [{"name": "plan", "type": "string", "description": "The plan the caller names"}]
    turn_data = FakeTurnData(lines=(GOLD,)).then({"plan": "platinum", "seats": 4}, PLATINUM)
    rig = _rig(_pathway(extractVars=extract, extractVarsMode="before_response"), turn_data)
    rig.source.reply("Hello.").reply("Diamond it is.")
    rig.port.script("extract_variables", {"plan": "diamond"})
    await rig.start()

    await rig.say("put me on diamond")

    assert rig.state.variables == {"plan": "diamond", "seats": 4}
    assert "Help with the diamond plan." in _system(rig)


async def test_a_port_that_raises_lets_the_turn_go_on_with_the_earlier_values() -> None:
    stream = json_log_stream()
    turn_data = FakeTurnData(lines=(GOLD,)).failing(RuntimeError("boom"))

    rig, actions = await _asked(turn_data, "You are on gold.")

    assert actions == [("stream", "You are on gold.", "t1.1", None)]
    assert rig.source.streams[-1].committed  # nothing changed: the speculative reply is the reply
    assert rig.state.variables == {"plan": "gold"}
    assert _system(rig).endswith(f"\n\n## Context\n{GOLD}")
    assert [line["event"] for line in log_events(stream)].count("engine.turn_data_failed") == 1


async def test_a_turn_cut_during_the_refresh_merges_nothing_and_the_next_turn_refreshes() -> None:
    hold = asyncio.Event()
    turn_data = FakeTurnData(lines=(GOLD,), hold=hold).then({"plan": "platinum"}, PLATINUM)
    rig = _rig(_pathway(), turn_data)
    rig.source.reply("Hello.").reply("speculative").reply("speculative").reply("You are on platinum.")
    await rig.start()
    cut = asyncio.create_task(rig.say("which plan"))
    async with asyncio.timeout(5):
        await turn_data.started.wait()

    cut.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cut

    assert turn_data.cancelled == 1
    assert rig.state.variables == {"plan": "gold"}
    assert turn_data.sections() == (GOLD,)
    assert turn_data.applied == []

    hold.set()
    merged = user("which plan am I on", rig.state.turn_index + 1, supersedes=rig.state.turn_index)
    actions = await drive(rig.engine.handle_user_turn(merged))

    assert actions == [("stream", "You are on platinum.", "t2.1", None)]
    assert len(turn_data.scopes) == 2
    assert rig.state.variables == {"plan": "platinum"}
    assert _system(rig).endswith(f"\n\n## Context\n{PLATINUM}")


async def _decision_pending(port: KeyedPort, turn_data: FakeTurnData, rig: Rig) -> asyncio.Task[Any]:
    """A caller turn whose refresh already finished while its routing call is still held."""
    turn = asyncio.create_task(rig.say("which plan"))
    async with asyncio.timeout(5):
        await port.started.wait()
        await turn_data.fetched.wait()
    assert not turn.done()
    return turn


def _trace(rig: Rig, turn_data: FakeTurnData) -> tuple[Any, ...]:
    """Everything a refresh can leave behind: the variables, the lines, what was reported."""
    return dict(rig.state.variables), turn_data.sections(), list(turn_data.applied)


async def test_a_turn_cut_after_its_refresh_finished_keeps_nothing_of_it() -> None:
    """The common cut: a cached or fast refresh is done in milliseconds, the decision still runs."""
    gate = asyncio.Event()
    port = KeyedPort(gates={"select_path": gate})
    turn_data = FakeTurnData(lines=(GOLD,)).then({"plan": "platinum"}, PLATINUM)
    turn_data.then({"plan": "silver"}, "Their plan is silver.")
    rig = _rig(_pathway(), turn_data, port=port)
    rig.source.reply("Hello.").reply("speculative").reply("speculative").reply("You are on silver.")
    await rig.start()
    cut = await _decision_pending(port, turn_data, rig)

    cut.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cut

    assert _trace(rig, turn_data) == ({"plan": "gold"}, (GOLD,), [])

    gate.set()
    merged = user("which plan am I on", rig.state.turn_index + 1, supersedes=rig.state.turn_index)
    actions = await drive(rig.engine.handle_user_turn(merged))

    assert actions == [("stream", "You are on silver.", "t2.1", None)]
    assert _trace(rig, turn_data) == (
        {"plan": "silver"},
        ("Their plan is silver.",),
        [{"plan": "silver"}],
    )
    assert _system(rig).endswith("\n\n## Context\nTheir plan is silver.")


async def test_a_turn_whose_decision_fails_after_the_refresh_finished_keeps_nothing_of_it() -> None:
    gate = asyncio.Event()
    port = KeyedPort(gates={"select_path": gate}).script("select_path", RuntimeError("boom"))
    turn_data = FakeTurnData(lines=(GOLD,)).then({"plan": "platinum"}, PLATINUM)
    rig = _rig(_pathway(), turn_data, port=port)
    rig.source.reply("Hello.").reply("speculative")
    await rig.start()
    failing = await _decision_pending(port, turn_data, rig)

    gate.set()
    with pytest.raises(ExceptionGroup) as raised:
        await failing

    assert raised.group_contains(RuntimeError, match="boom")
    assert _trace(rig, turn_data) == ({"plan": "gold"}, (GOLD,), [])
