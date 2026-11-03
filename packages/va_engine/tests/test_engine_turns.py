"""PathwayEngine speculation, loop protection and interruptions (specs/11 §5.1, §5.5, §5.6, §5.8;
Decision P07-T08b)."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from dialogue_helpers import unavailable
from engine_helpers import Rig, condition, drive, engine_rig, first_stream, route, user
from graph_helpers import edge, graph, node, start

from va_engine.engine import LOOP_PROTECTION, new_pathway_state
from va_engine.engine_decide import TurnDecision
from va_engine.models import HISTORY_CAP, PathwayGraph, TurnRecord
from va_engine.navigation import ActiveGraph
from va_engine.router import RouteDecision

ERROR = "I'm sorry, I'm having trouble right now."


def two_steps(**start_data: object) -> PathwayGraph:
    return graph(
        [start("a", prompt="Help the caller.", **start_data), node("b", prompt="Wrap up.")],
        [edge("e_ab", "a", "b", "Caller is done")],
    )


async def started(pathway: PathwayGraph, **rig_options: object) -> Rig:
    rig = engine_rig(pathway, **rig_options)  # type: ignore[arg-type]
    rig.source.reply("Hello.")
    await rig.start()
    return rig


# Speculation (§5.8).


async def test_speculation_hit_commits_the_buffered_stream() -> None:
    rig = await started(two_steps())
    rig.source.reply("Sure, ", "go on.")
    actions = await rig.say("I have a question")
    assert actions == [("stream", "Sure, go on.", "t1.1", None)]
    assert len(rig.source.streams) == 2
    speculative = rig.source.streams[1]
    assert speculative.request.speculative
    assert speculative.committed
    assert not speculative.discarded
    assert rig.history()[-1] == ("assistant", "Sure, go on.")


async def test_speculation_miss_on_route_change_requests_the_new_node() -> None:
    rig = await started(two_steps())
    rig.port.script("select_path", route("e_ab", 9))
    rig.source.reply("stale").reply("Bye now.")
    actions = await rig.say("That's all")
    assert actions == [("stream", "Bye now.", "t1.1", None)]
    speculative, fresh = rig.source.streams[1:]
    assert speculative.discarded
    assert speculative.closed
    assert not fresh.request.speculative
    assert "## Current step: b" in (fresh.request.messages[0].content or "")


async def test_speculation_miss_on_an_unmet_condition() -> None:
    rig = await started(two_steps(condition="The caller gave their name", conditionThreshold=6))
    rig.port.script("score_condition", condition(4))
    rig.source.reply("stale").reply("May I have your name?")
    actions = await rig.say("Hi")
    assert actions == [("stream", "May I have your name?", "t1.1", None)]
    speculative, fresh = rig.source.streams[1:]
    assert speculative.discarded
    assert "The caller gave their name" in (fresh.request.messages[0].content or "")


async def test_speculation_disabled_by_the_org_setting() -> None:
    rig = await started(two_steps(), speculative=False)
    rig.source.reply("Sure.")
    await rig.say("Question")
    assert [stream.request.speculative for stream in rig.source.streams] == [False, False]


async def test_no_speculation_on_static_text_nodes() -> None:
    pathway = graph([start("a", text="Hello there.")])
    rig = await started(pathway)
    await rig.say("Hi")
    assert rig.source.streams == []


# Loop protection (§5.5, §5.6).


def bouncing(*, fallback: bool) -> PathwayGraph:
    edges = [edge("e_ab", "a", "b", "Go to b"), edge("e_ba", "b", "a", "Back to a")]
    if fallback:
        edges.append(edge("e_bc", "b", "c", "", isFallback=True))
    return graph(
        [start("a", text="At a."), node("b", text="At b.", maxVisits=1), node("c", text="At c.")], edges
    )


async def bounce(rig: Rig) -> list[tuple[object, ...]]:
    rig.port.script("select_path", route("e_ab", 9), route("e_ba", 9), route("e_ab", 9))
    await rig.start()
    await rig.say("b please")
    await rig.say("back")
    return await rig.say("b again")


async def test_max_visits_takes_the_fallback_edge() -> None:
    rig = engine_rig(bouncing(fallback=True))
    actions = await bounce(rig)
    assert actions == [("say", "At c.", "t3.1", None)]
    assert rig.state.current_node_id == "c"
    assert [data["to_node"] for data in rig.events.of("call.route")][-2:] == ["b", "c"]


async def test_max_visits_without_fallback_ends_the_call() -> None:
    rig = engine_rig(bouncing(fallback=False))
    actions = await bounce(rig)
    assert actions == [("say", ERROR, "t3.1", None), ("end", LOOP_PROTECTION, "system", "t3.1")]
    assert rig.state.ended
    assert rig.state.end_reason == LOOP_PROTECTION
    assert rig.history()[-1] == ("assistant", ERROR)


async def test_chain_depth_limit_ends_a_cycle_of_skipping_nodes() -> None:
    pathway = graph(
        [
            start("x", text="X.", modelOptions={"skipUserResponse": True}, maxVisits=100),
            node("y", text="Y.", modelOptions={"skipUserResponse": True}, maxVisits=100),
        ],
        [edge("e_xy", "x", "y", "next", alwaysPick=True), edge("e_yx", "y", "x", "next", alwaysPick=True)],
    )
    rig = engine_rig(pathway)
    actions = await rig.start()
    texts = [action[1] for action in actions if action[0] == "say"]
    assert texts == ["X.", "Y."] * 5 + ["X.", ERROR]
    assert actions[-1] == ("end", LOOP_PROTECTION, "system", "t0.12")
    assert rig.port.calls == []
    assert rig.state.end_reason == LOOP_PROTECTION


async def test_max_total_turns_ends_before_any_decision() -> None:
    pathway = graph([start("a", text="Hi.")], [], {"maxTotalTurns": 1})
    rig = engine_rig(pathway)
    await rig.start()
    await rig.say("one")
    calls = len(rig.port.calls)
    actions = await rig.say("two")
    assert actions == [("say", ERROR, "t2.1", None), ("end", LOOP_PROTECTION, "system", "t2.1")]
    assert len(rig.port.calls) == calls


async def test_blank_error_phrase_ends_without_a_line() -> None:
    pathway = graph([start("a", text="Hi.")], [], {"maxTotalTurns": 1, "errorPhrase": " "})
    rig = engine_rig(pathway)
    await rig.say("one")
    assert await rig.say("two") == [("end", LOOP_PROTECTION, "system", None)]


# Interruptions and failures (§5.1).


async def test_barge_in_keeps_the_partial_then_the_heard_text() -> None:
    rig = engine_rig(two_steps())
    rig.source.reply("Hello ", "there, ", "how are you?")
    turn = rig.engine.start()
    stream = await first_stream(turn)
    assert await anext(stream.tokens) == "Hello "
    await turn.aclose()
    assert rig.history() == [("assistant", "Hello ")]
    await rig.engine.on_interrupted("Hel—")
    assert rig.state.history[-1].content == "Hel—"
    assert rig.state.history[-1].interrupted
    await rig.engine.on_interrupted("again")
    assert rig.state.history[-1].content == "Hel—"


async def test_blank_heard_text_becomes_the_dash() -> None:
    rig = await started(two_steps())
    await rig.engine.on_interrupted("  ")
    assert rig.state.history[-1].content == "—"
    assert rig.state.history[-1].interrupted


async def test_closing_before_the_stream_was_read_records_nothing() -> None:
    rig = engine_rig(two_steps())
    rig.source.reply("Hello.")
    turn = rig.engine.start()
    await first_stream(turn)
    await turn.aclose()
    assert rig.history() == []


async def test_superseding_turn_replaces_the_earlier_one() -> None:
    rig = await started(two_steps())
    rig.source.reply("Reply one.")
    await drive(rig.engine.handle_user_turn(user("I want", 1)))
    rig.source.reply("Reply two.")
    await drive(rig.engine.handle_user_turn(user("I want to order", 2, supersedes=1)))
    assert rig.history() == [
        ("assistant", "Hello."),
        ("user", "I want to order"),
        ("assistant", "Reply two."),
    ]
    await drive(rig.engine.handle_user_turn(user("again", 3, supersedes=9)))
    assert rig.history()[-1] == ("user", "again")


async def test_unavailable_model_speaks_the_error_phrase() -> None:
    rig = engine_rig(two_steps())
    rig.source.reply(fail=unavailable())
    actions = await rig.start()
    assert actions == [("stream", "", "t0.1", None), ("say", ERROR, "t0.2", None)]
    assert rig.history() == [("assistant", ERROR)]


async def test_aclose_closes_an_open_speculation() -> None:
    rig = await started(two_steps())
    gate = rig.port.gates["select_path"] = asyncio.Event()
    rig.source.reply("never")
    turn = rig.engine.handle_user_turn(user("Hi", 1))
    task = asyncio.create_task(anext(turn))
    await rig.port.started.wait()
    task.cancel()
    with contextlib.suppress(BaseException):
        await task
    gate.set()
    await rig.engine.aclose()
    assert rig.source.streams[-1].discarded


@pytest.mark.parametrize("channel", ["voice", "sms"])
async def test_text_and_voice_channels_share_the_flow(channel: str) -> None:
    rig = engine_rig(graph([start("a", text="Hi {{from}}.")]), channel=channel)  # type: ignore[arg-type]
    assert await rig.start() == [("say", "Hi +14155550100.", "t0.1", None)]


async def test_fallback_target_over_its_limit_too_ends_the_call() -> None:
    pathway = graph(
        [
            start("a", text="At a."),
            node("b", text="At b.", maxVisits=1),
            node("c", text="At c.", maxVisits=1),
        ],
        [
            edge("e_ab", "a", "b", "Go to b"),
            edge("e_ba", "b", "a", "Back to a"),
            edge("e_bc", "b", "c", "", isFallback=True),
            edge("e_ca", "c", "a", "Back to a"),
        ],
    )
    rig = engine_rig(pathway)
    assert (await bounce(rig))[0] == ("say", "At c.", "t3.1", None)
    rig.port.script("select_path", route("e_ca", 9), route("e_ab", 9))
    await rig.say("back again")
    actions = await rig.say("b once more")
    assert actions[-1] == ("end", LOOP_PROTECTION, "system", "t5.1")


async def test_unavailable_model_with_a_blank_error_phrase_says_nothing_more() -> None:
    rig = engine_rig(graph([start("a", prompt="Talk.")], [], {"errorPhrase": ""}))
    rig.source.reply(fail=unavailable())
    assert await rig.start() == [("stream", "", "t0.1", None)]
    assert rig.history() == []


async def test_an_ended_conversation_does_not_start_again() -> None:
    rig = engine_rig(graph([start("a", text="Hi.")]))
    rig.state.ended = True
    assert await rig.start() == []


def test_a_graph_without_start_node_has_no_fresh_state() -> None:
    with pytest.raises(ValueError, match="no start node"):
        new_pathway_state(ActiveGraph(None, None, graph([node("a")])))


def test_turn_decision_without_a_condition_has_no_suffix() -> None:
    decision = TurnDecision(route=RouteDecision(kind="stay", from_node="a", to_node="a"))
    assert not decision.held
    assert decision.suffix is None


async def test_superseding_after_the_history_cap_drops_the_right_records() -> None:
    rig = await started(two_steps())
    for i in range(HISTORY_CAP + 5):
        rig.state.add_turn(TurnRecord(role="assistant", content=f"filler {i}", node_id="a", at_ms=i))
    rig.source.reply("Reply one.")
    await drive(rig.engine.handle_user_turn(user("first", 1)))
    rig.source.reply("Reply two.")
    await drive(rig.engine.handle_user_turn(user("first and more", 2, supersedes=1)))
    history = rig.history()
    assert ("user", "first") not in history
    assert ("assistant", "Reply one.") not in history
    assert history[-2:] == [("user", "first and more"), ("assistant", "Reply two.")]
    assert len(rig.state.history) <= HISTORY_CAP


async def test_only_the_latest_turn_can_be_superseded() -> None:
    rig = await started(two_steps())
    rig.source.reply("Reply one.")
    await drive(rig.engine.handle_user_turn(user("one", 1)))
    rig.source.reply("Reply two.")
    await drive(rig.engine.handle_user_turn(user("two", 2)))
    rig.source.reply("Reply three.")
    await drive(rig.engine.handle_user_turn(user("three", 3, supersedes=1)))
    assert ("user", "one") in rig.history()
