"""Custom and system tools on pathway reply nodes (specs/13 §4/§5/§6; Decisions P08-T02c1, P08-FIX-16,
P08-FIX-17, P08-FIX-19, P08-FIX-20, P08-FIX-21)."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
from typing import Any

import pytest
from dialogue_helpers import unavailable
from engine_helpers import Rig, closed_on, closed_on_line, drive, engine_rig, route, user
from graph_helpers import edge, graph, node, start
from handler_helpers import spoken
from service_helpers import json_log_stream, log_events
from task_helpers import RecordingExecutor

from va_engine import engine as engine_module
from va_engine.engine_node_tools import NO_OFFER, NodeTools
from va_engine.models import PathwayGraph, Say, SendDtmfAction
from va_engine.system_tools import SystemToolFacts
from va_engine.task_tools import IN_PROGRESS
from va_llm.errors import LlmConnectionError
from va_llm.models import ToolCall, ToolDef
from va_voice.phrase_cache import TOOL_FILLERS
from va_voice.pipeline import UserTurn

LOOKUP = ToolDef(
    name="lookup",
    description="Look up opening hours",
    parameters={"type": "object", "properties": {"city": {"type": "string"}}},
)
TRACK = ToolDef(name="track", description="Track an order", parameters={"type": "object"})
VOICE = SystemToolFacts(runner="pathway", channel="voice")
ERROR = "I'm sorry, I'm having trouble right now."


def executor(**over: Any) -> RecordingExecutor:
    values: dict[str, Any] = {
        "tools": (LOOKUP, TRACK),
        "names": frozenset({"lookup", "track"}),
        "refs": {"TL-1": "lookup", "TL-2": "track"},
    }
    values.update(over)
    return RecordingExecutor(**values)


def call(call_id: str, name: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments_json=json.dumps(arguments))


def one_node(global_config: dict[str, Any] | None = None, **data: Any) -> PathwayGraph:
    """A one-node pathway: a Default start node offering ``TL-1`` unless ``tools`` is given."""
    return graph([start("main", **({"tools": ["TL-1"]} | data))], [], global_config)


def tool_records(rig: Rig) -> list[tuple[str | None, str]]:
    return [(record.tool_name, record.content) for record in rig.state.history if record.role == "tool"]


def offered(rig: Rig) -> list[list[str]]:
    return [[tool.name for tool in request.tools] for request in rig.source.requests]


async def test_a_node_tool_is_offered_and_its_answer_feeds_the_next_reply() -> None:
    tools = executor(results={"lookup": '{"status":200,"data":"9-5"}'})
    rig = engine_rig(one_node(), tools=tools, fillers=False)
    rig.source.reply("Checking. ", calls=[call("c1", "lookup", city="Paris")]).reply("We open at nine.")

    actions = await rig.start()

    assert actions == [("stream", "Checking. ", "t0.1", None), ("stream", "We open at nine.", "t0.2", None)]
    assert offered(rig) == [["lookup"], ["lookup"]]
    assert [c.name for c in tools.calls] == ["lookup"]
    assert tool_records(rig) == [("lookup", '{"status":200,"data":"9-5"}')]
    second = [message.content or "" for message in rig.source.requests[1].messages]
    assert any('Tool lookup returned: {"status":200,"data":"9-5"}' in text for text in second)


async def test_global_tools_join_the_node_tools_on_reply_nodes_only() -> None:
    pathway = graph(
        [
            start("main", tools=["TL-1"]),
            node("kb", node_type="Knowledge Base", prompt="Answer from the docs."),
            node("wait", node_type="Wait for Response", prompt="Ask again."),
        ],
        [],
        {"globalTools": ["TL-2", "TL-1", "TL-missing"]},
    )
    tools = NodeTools(executor(), VOICE, clock=engine_rig(pathway).clock, fillers=True)
    stream = json_log_stream()

    main = tools.offer(pathway.nodes[0], pathway)
    kb = tools.offer(pathway.nodes[1], pathway)

    assert [tool.name for tool in main.tools] == ["end_call", "press_button", "lookup", "track"]
    assert [tool.name for tool in kb.tools] == ["end_call", "press_button", "track", "lookup"]
    assert tools.offer(pathway.nodes[2], pathway) is NO_OFFER
    assert main.custom == frozenset({"lookup", "track"})
    unresolved = [event for event in log_events(stream) if event["event"] == "engine.tool_unresolved"]
    assert [event["count"] for event in unresolved] == [1, 1]


async def test_scheduling_nodes_keep_only_their_booking_tools() -> None:
    pathway = graph(
        [start("book", node_type="Scheduling", prompt="Book.", scheduling={"slots": []}, tools=["TL-1"])],
        [],
        {"globalTools": ["TL-2"]},
    )
    rig = engine_rig(pathway, tools=executor(), system=VOICE)
    rig.source.reply("When suits you?")

    await rig.start()

    assert offered(rig) == [["list_slots", "book_slot"]]


async def test_custom_tools_named_like_system_tools_or_twice_are_skipped() -> None:
    clash = ToolDef(name="end_call", parameters={"type": "object"})
    tools = NodeTools(
        executor(tools=(LOOKUP, clash), refs={"TL-1": "lookup", "TL-9": "end_call"}),
        None,
        clock=engine_rig(one_node()).clock,
        fillers=True,
    )
    pathway = one_node(tools=["TL-1", "TL-9", {"name": "lookup"}])

    assert [tool.name for tool in tools.offer(pathway.nodes[0], pathway).tools] == ["lookup"]


@pytest.mark.parametrize(
    ("allowed", "names"), [(True, ["end_call", "press_button"]), (False, ["press_button"])]
)
async def test_end_call_is_offered_only_when_the_graph_allows_it(allowed: bool, names: list[str]) -> None:
    rig = engine_rig(one_node(global_config={"allowEndCallTool": allowed}, tools=[]), system=VOICE)
    rig.source.reply("Bye?", calls=[call("c1", "end_call", reason="done")]).reply("Still here.")

    actions = await rig.start()

    assert offered(rig)[0] == names
    if allowed:
        assert rig.state.ended
        assert actions[-1] == ("end", "agent_end_call", "agent", None)
    else:
        assert not rig.state.ended
        assert tool_records(rig) == [("end_call", '{"error":"invalid_arguments","detail":"unknown_tool"}')]
        assert actions[-1] == ("stream", "Still here.", "t0.2", None)


@pytest.mark.parametrize(
    ("reply", "arguments", "line"),
    [
        ("Okay. ", {"final_message": "Bye now."}, "Bye now."),
        ("", {}, "Goodbye."),
        ("Okay. ", {}, None),
    ],
)
async def test_end_call_speaks_its_final_line_and_ends(
    reply: str, arguments: dict[str, str], line: str | None
) -> None:
    rig = engine_rig(one_node(tools=[]), system=VOICE)
    rig.source.reply(*([reply] if reply else []), calls=[call("c1", "end_call", reason="done", **arguments)])

    actions = await rig.start()

    tail = (
        [("say", line, "t0.2", False), ("end", "agent_end_call", "agent", "t0.2")]
        if line
        else [("end", "agent_end_call", "agent", None)]
    )
    assert actions[-len(tail) :] == tail
    assert (rig.state.ended, rig.state.end_reason) == (True, "agent_end_call")
    assert tool_records(rig) == [("end_call", '{"status":"ending"}')]
    assert len(rig.source.requests) == 1


GOODBYE = ("say", "Goodbye.", "t1.2", False)
ENDED = ("end", "agent_end_call", "agent", "t1.2")


async def ending_turn(rig: Rig) -> list[tuple[Any, ...]]:
    """The opener, then a caller turn the model answers with ``end_call``: the reply is closed on its final
    line (a caller turn ended before the line's first audio), so the end was never received."""
    rig.source.reply("Hello.").reply("", calls=[call("c1", "end_call", reason="done")])
    await rig.start()
    return await closed_on_line(rig.engine.handle_user_turn(user("that is all", 1)))


@pytest.mark.parametrize("supersedes", [1, None])
async def test_an_end_call_closed_on_its_final_line_ends_on_the_next_turn(supersedes: int | None) -> None:
    rig = engine_rig(one_node(tools=[]), system=VOICE)
    stream = json_log_stream()

    before = await ending_turn(rig)
    history = rig.history()
    merged = user("that is all, thanks", 2, supersedes=supersedes)
    owed = await drive(rig.engine.handle_user_turn(merged))
    later = await rig.say("hello?")

    assert before == [("stream", "", "t1.1", None), GOODBYE]
    assert owed == [GOODBYE, ENDED]
    assert later == []
    assert (rig.state.ended, rig.state.end_reason, rig.state.turn_index) == (True, "agent_end_call", 1)
    assert rig.history() == history
    assert history[-2:] == [("tool", '{"status":"ending"}'), ("assistant", "Goodbye.")]
    assert len(rig.source.requests) == 2
    after_end = [event["owed"] for event in log_events(stream) if event["event"] == "engine.turn_after_end"]
    assert after_end == [True, False]


async def test_an_owed_end_closed_again_on_its_line_stays_owed() -> None:
    rig = engine_rig(one_node(tools=[]), system=VOICE)
    await ending_turn(rig)

    again = await closed_on_line(rig.engine.handle_user_turn(user("wait", 2, supersedes=1)))
    owed = await drive(rig.engine.handle_user_turn(user("wait a moment", 3, supersedes=2)))

    assert again == [GOODBYE]
    assert owed == [GOODBYE, ENDED]
    assert await rig.say("hello?") == []


async def test_a_delivered_end_is_not_repeated() -> None:
    rig = engine_rig(one_node(tools=[]), system=VOICE)
    rig.source.reply("", calls=[call("c1", "end_call", reason="done")])

    actions = await rig.start()

    assert actions[-1] == ("end", "agent_end_call", "agent", "t0.2")
    assert await rig.say("hello?") == []
    assert len(rig.source.requests) == 1


# An answer that reports an action is on record only once the action was handed over (Decision P08-FIX-19).

SENT_1 = ("press_button", '{"status":"sent","digits":"1"}')
SENT_2 = ("press_button", '{"status":"sent","digits":"2"}')
CUT = '{"error":"interrupted"}'


def press(call_id: str, digits: str) -> ToolCall:
    return call(call_id, "press_button", digits=digits)


async def test_press_button_sends_dtmf_right_after_its_answer() -> None:
    rig = engine_rig(one_node(tools=[]), system=VOICE)
    rig.source.reply("", calls=[press("c1", "1#")]).reply("Pressed.")

    actions = await rig.start()

    assert actions == [
        ("stream", "", "t0.1", None),
        ("dtmf", "1#", 250, None),
        ("stream", "Pressed.", "t0.2", None),
    ]
    assert tool_records(rig) == [("press_button", '{"status":"sent","digits":"1#"}')]


async def test_presses_are_answered_and_sent_in_call_order() -> None:
    hours = '{"hours":"9-5"}'
    rig = engine_rig(one_node(), tools=executor(results={"lookup": hours}), system=VOICE, fillers=False)
    rig.source.reply("", calls=[press("c1", "1"), call("c2", "lookup"), press("c3", "2")]).reply("Pressed.")
    rendered: list[tuple[Any, ...]] = []
    on_record: list[list[tuple[str | None, str]]] = []

    async with contextlib.aclosing(rig.engine.start()) as actions:
        async for action in actions:
            rendered.extend(await spoken((action,)))
            if isinstance(action, SendDtmfAction):
                on_record.append(tool_records(rig))

    assert rendered == [
        ("stream", "", "t0.1", None),
        ("dtmf", "1", 250, None),
        ("dtmf", "2", 250, None),
        ("stream", "Pressed.", "t0.2", None),
    ]
    assert on_record == [[SENT_1], [SENT_1, ("lookup", hours), SENT_2]]


async def test_a_reply_closed_at_the_first_press_answers_the_later_press_interrupted() -> None:
    rig = engine_rig(one_node(tools=[]), system=VOICE)
    rig.source.reply("", calls=[press("c1", "1"), press("c2", "2")])

    seen = await closed_on(rig.engine.start(), SendDtmfAction)

    assert seen == [("stream", "", "t0.1", None), ("dtmf", "1", 250, None)]
    assert tool_records(rig) == [SENT_1, ("press_button", CUT)]


async def test_a_superseded_turn_keeps_the_sent_and_the_interrupted_press() -> None:
    rig = engine_rig(one_node(tools=[]), system=VOICE)
    rig.source.reply("Hello.").reply("", calls=[press("c1", "1"), press("c2", "2")]).reply("Okay.")
    await rig.start()
    await closed_on(rig.engine.handle_user_turn(user("press one", 1)), SendDtmfAction)

    await drive(rig.engine.handle_user_turn(user("press one two", 2, supersedes=1)))

    assert rig.history() == [
        ("assistant", "Hello."),
        ("tool", SENT_1[1]),
        ("tool", CUT),
        ("user", "press one two"),
        ("assistant", "Okay."),
    ]


def press_then_end(rig: Rig) -> None:
    """The opener, then a reply that presses ``1`` and ends the call in one round."""
    ending = call("c2", "end_call", reason="done", final_message="Bye.")
    rig.source.reply("Hello.").reply("", calls=[press("c1", "1"), ending])


async def test_an_end_call_behind_a_press_closed_at_the_press_does_not_end() -> None:
    rig = engine_rig(one_node(tools=[]), system=VOICE)
    press_then_end(rig)
    rig.source.reply("Still here.")
    await rig.start()

    seen = await closed_on(rig.engine.handle_user_turn(user("press one", 1)), SendDtmfAction)
    later = await rig.say("hello?")

    assert seen == [("stream", "", "t1.1", None), ("dtmf", "1", 250, None)]
    assert tool_records(rig) == [SENT_1, ("end_call", CUT)]
    assert (rig.state.ended, rig.state.end_reason) == (False, None)
    assert later == [("stream", "Still here.", "t2.1", None)]
    assert len(rig.source.requests) == 3


async def test_an_end_call_behind_a_press_handed_over_ends_the_call() -> None:
    rig = engine_rig(one_node(tools=[]), system=VOICE)
    press_then_end(rig)
    await rig.start()

    actions = await drive(rig.engine.handle_user_turn(user("press one", 1)))

    assert actions == [
        ("stream", "", "t1.1", None),
        ("dtmf", "1", 250, None),
        ("say", "Bye.", "t1.2", False),
        ("end", "agent_end_call", "agent", "t1.2"),
    ]
    assert tool_records(rig) == [SENT_1, ("end_call", '{"status":"ending"}')]
    assert (rig.state.ended, rig.state.end_reason) == (True, "agent_end_call")


async def test_transfer_call_is_never_offered_on_pathways() -> None:
    facts = SystemToolFacts(runner="task", channel="voice", transfer_phone_number="+14155550199")
    rig = engine_rig(one_node(tools=[]), system=facts)
    rig.source.reply("", calls=[call("c1", "transfer_call")]).reply("I cannot transfer you.")

    await rig.start()

    assert "transfer_call" not in offered(rig)[0]
    assert tool_records(rig) == [("transfer_call", '{"error":"invalid_arguments","detail":"unknown_tool"}')]


async def test_tool_speech_is_spoken_before_the_follow_up_and_variables_merge() -> None:
    tools = executor(
        speeches={"lookup": "Looking up {{input.city}}."}, variables={"lookup": {"hours": "9-5"}}
    )
    rig = engine_rig(one_node(), tools=tools)
    rig.source.reply("", calls=[call("c1", "lookup", city="Paris")]).reply("Open {{hours}}.")

    actions = await rig.start()

    assert actions[1] == ("say", "Looking up Paris.", "t0.2", None)
    assert rig.state.variables["hours"] == "9-5"
    assert ("assistant", "Looking up Paris.") in rig.history()


async def drive_held(
    rig: Rig, tools: RecordingExecutor, *, close_on_line: bool, turn: UserTurn | None = None
) -> list[tuple[Any, ...]]:
    """Start the conversation (or run the caller's ``turn``) with held tools: release the waiting ones (or
    close the turn) at each line."""
    rendered: list[tuple[Any, ...]] = []
    running = rig.engine.start() if turn is None else rig.engine.handle_user_turn(turn)
    async with contextlib.aclosing(running) as actions:
        async for action in actions:
            rendered.extend(await spoken((action,)))
            if isinstance(action, Say):
                if close_on_line:
                    break
                tools.release.set()
                tools.release.clear()
    return rendered


async def test_a_held_tool_gets_a_round_robin_filler() -> None:
    tools = executor(hold=True)
    rig = engine_rig(one_node(), tools=tools)
    for index in range(2):
        rig.source.reply("", calls=[call(f"c{index}", "lookup")])
    rig.source.reply("Done.")

    actions = await drive_held(rig, tools, close_on_line=False)

    assert [item[1] for item in actions if item[0] == "say"] == list(TOOL_FILLERS[:2])


async def test_fillers_off_silences_speech_and_fillers() -> None:
    tools = executor(hold=True, speeches={"lookup": "Looking it up."})
    tools.release.set()
    rig = engine_rig(one_node(), tools=tools, fillers=False)
    rig.source.reply("", calls=[call("c1", "lookup")]).reply("Done.")

    actions = await rig.start()

    assert [item[0] for item in actions] == ["stream", "stream"]


async def test_a_barge_in_during_the_filler_cancels_the_tool_and_answers_it_interrupted() -> None:
    tools = executor(hold=True)
    rig = engine_rig(one_node(), tools=tools)
    rig.source.reply("", calls=[call("c1", "lookup"), call("c2", "nope")])

    actions = await drive_held(rig, tools, close_on_line=True)

    assert actions[-1][0] == "say"
    assert [name for name, _ in tools.cancelled] == ["lookup"]
    assert tool_records(rig) == [
        ("lookup", '{"error":"interrupted"}'),
        ("nope", '{"error":"invalid_arguments","detail":"unknown_tool"}'),
    ]


async def test_two_calls_without_ids_get_their_own_answers() -> None:
    tools = executor(
        results={"lookup": '{"hours":"9-5"}', "track": '{"eta":"Friday"}'},
        variables={"lookup": {"hours": "9-5"}, "track": {"eta": "Friday"}},
    )
    rig = engine_rig(one_node(tools=["TL-1", "TL-2"]), tools=tools, fillers=False)
    rig.source.reply("", calls=[call("", "lookup"), call("", "track")]).reply("Done.")

    await rig.start()

    assert [c.name for c in tools.calls] == ["lookup", "track"]
    assert tool_records(rig) == [("lookup", '{"hours":"9-5"}'), ("track", '{"eta":"Friday"}')]
    assert (rig.state.variables["hours"], rig.state.variables["eta"]) == ("9-5", "Friday")


async def test_a_barge_in_cancels_and_answers_every_call_without_an_id() -> None:
    tools = executor(hold=True)
    rig = engine_rig(one_node(tools=["TL-1", "TL-2"]), tools=tools)
    rig.source.reply("", calls=[call("", "lookup"), call("", "track")])

    actions = await drive_held(rig, tools, close_on_line=True)

    assert actions[-1][0] == "say"
    assert sorted(name for name, _ in tools.cancelled) == ["lookup", "track"]
    assert tools.active == 0
    assert tool_records(rig) == [
        ("lookup", '{"error":"interrupted"}'),
        ("track", '{"error":"interrupted"}'),
    ]


async def test_the_fourth_reply_of_a_pass_offers_no_tools() -> None:
    rig = engine_rig(one_node(), tools=executor(), fillers=False)
    for index in range(3):
        rig.source.reply("", calls=[call(f"c{index}", "lookup")])
    rig.source.reply("Here you go.", calls=[call("c9", "lookup")])

    actions = await rig.start()

    assert offered(rig) == [["lookup"], ["lookup"], ["lookup"], []]
    assert actions[-1] == ("stream", "Here you go.", "t0.4", None)
    assert len(tool_records(rig)) == 3


async def test_two_invalid_rounds_stop_offering_tools() -> None:
    rig = engine_rig(one_node(tools=[]), system=VOICE)
    for index in range(2):
        rig.source.reply("", calls=[call(f"c{index}", "press_button", digits="x")])
    rig.source.reply("Sorry.")

    await rig.start()

    assert [len(names) for names in offered(rig)] == [2, 2, 0]


async def test_executor_backed_system_tools_run_through_the_executor() -> None:
    tools = executor(names=frozenset({"lookup", "send_sms"}), results={"send_sms": '{"status":"sent"}'})
    facts = SystemToolFacts(runner="pathway", channel="voice", sms_capable=True)
    rig = engine_rig(one_node(tools=[]), tools=tools, system=facts, fillers=False)
    rig.source.reply("", calls=[call("c1", "send_sms", message="Hi")]).reply("Sent.")

    await rig.start()

    assert offered(rig)[0] == ["end_call", "press_button", "send_sms"]
    assert [c.name for c in tools.calls] == ["send_sms"]
    assert tool_records(rig) == [("send_sms", '{"status":"sent"}')]


async def test_without_an_executor_or_facts_no_tools_are_offered() -> None:
    rig = engine_rig(one_node())
    rig.source.reply("Hello.", calls=[call("c1", "lookup")])

    actions = await rig.start()

    assert offered(rig) == [[]]
    assert actions == [("stream", "Hello.", "t0.1", None)]
    assert tool_records(rig) == []


async def test_static_text_nodes_answer_no_tool_calls() -> None:
    rig = engine_rig(one_node(text="Welcome."), tools=executor())

    actions = await rig.start()

    assert actions == [("say", "Welcome.", "t0.1", None)]
    assert rig.source.requests == []


async def test_the_tool_usage_note_quotes_the_executor_speech() -> None:
    rig = engine_rig(one_node(), tools=executor(note="Checking that now."), fillers=False)
    rig.source.reply("Hi.")

    await rig.start()

    system = rig.source.requests[0].messages
    assert any("Checking that now." in (message.content or "") for message in system)


def scheduling_node() -> PathwayGraph:
    body = {"availabilityUrl": "https://cal.acme.test/slots", "bookingUrl": "https://cal.acme.test/book"}
    return graph([start("book", node_type="Scheduling", scheduling=body, prompt="Offer the slots.")], [])


@pytest.mark.parametrize("pathway", [one_node(), scheduling_node()], ids=["reply-node", "scheduling"])
async def test_an_unavailable_model_ends_the_turn_with_the_error_phrase(pathway: PathwayGraph) -> None:
    tools = executor()
    rig = engine_rig(pathway, tools=tools, system=VOICE)
    rig.source.reply(fail=unavailable())

    actions = await rig.start()

    assert actions == [("stream", "", "t0.1", None), ("say", ERROR, "t0.2", None)]
    assert offered(rig)[0] != []
    assert len(rig.source.requests) == 1
    assert (tool_records(rig), tools.calls) == ([], [])
    assert rig.history() == [("assistant", ERROR)]


async def test_the_calls_of_a_cut_off_reply_are_not_run() -> None:
    tools = executor()
    rig = engine_rig(one_node(), tools=tools, system=VOICE)
    cut = LlmConnectionError("reset", provider_id="platform", model="dialogue")
    partial = ToolCall(id="c1", name="lookup", arguments_json="{")
    rig.source.reply("Let me ", calls=[partial, call("c2", "end_call", reason="done")], error=cut)
    stream = json_log_stream()

    actions = await rig.start()

    assert actions == [("stream", "Let me ", "t0.1", None)]
    assert len(rig.source.requests) == 1
    assert (tool_records(rig), tools.calls, rig.state.ended) == ([], [], False)
    [event] = [event for event in log_events(stream) if event["event"] == "engine.llm_stream_failed"]
    assert event["error_type"] == "LlmConnectionError"


async def test_the_call_wide_cap_answers_the_excess_and_then_offers_no_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_module, "MAX_TOOL_CALLS_PER_CALL", 2)
    tools = executor()
    rig = engine_rig(one_node(), tools=tools, fillers=False, speculative=False)
    rig.source.reply("", calls=[call(f"c{index}", "lookup") for index in range(3)]).reply("Here you go.")
    rig.source.reply("Nothing more.", calls=[call("c9", "lookup")])

    await rig.start()
    await rig.say("And tomorrow?")

    assert offered(rig) == [["lookup"], [], []]
    assert [c.id for c in tools.calls] == ["c0", "c1"]
    assert tool_records(rig) == [
        ("lookup", '{"ok":"lookup"}'),
        ("lookup", '{"ok":"lookup"}'),
        ("lookup", '{"error":"too_many_tool_calls"}'),
    ]


async def test_a_static_node_never_runs_the_calls_of_an_earlier_stream() -> None:
    pathway = graph(
        [start("main", prompt="Help.", tools=["TL-1"]), node("static", text="All set.", tools=["TL-1"])],
        [edge("e1", "main", "static", "Caller is done")],
    )
    tools = executor()
    rig = engine_rig(pathway, tools=tools, fillers=False, speculative=False)
    for index in range(3):
        rig.source.reply("", calls=[call(f"c{index}", "lookup")])
    rig.source.reply("Here you go.", calls=[call("c9", "lookup")])
    await rig.start()
    rig.port.script("select_path", route("e1", 9))

    actions = await rig.say("Thanks, that is all.")

    assert [item[:2] for item in actions] == [("say", "All set.")]
    assert [c.id for c in tools.calls] == ["c0", "c1", "c2"]
    assert len(tool_records(rig)) == 3


# A superseded turn keeps the answers of the tools it ran (Decision P08-FIX-17 item 1).


def context_update(name: str, content: str) -> str:
    return f"Tool {name} returned: {content}"


def position(rig: Rig, role: str, text: str) -> int:
    """Where the last request holds a ``role`` message containing ``text``."""
    messages = rig.source.requests[-1].messages
    [found] = [
        index
        for index, message in enumerate(messages)
        if message.role == role and text in (message.content or "")
    ]
    return found


async def test_a_superseded_turn_keeps_its_tool_records() -> None:
    result = '{"status":200,"data":"9-5"}'
    tools = executor(results={"lookup": result}, variables={"lookup": {"hours": "9-5"}})
    rig = engine_rig(one_node(), tools=tools, fillers=False)
    rig.source.reply("Hello.").reply("", calls=[call("c1", "lookup", city="Paris")])
    rig.source.reply("It opens at nine.").reply("Both open at nine.")
    await rig.start()
    await drive(rig.engine.handle_user_turn(user("paris", 1)))

    await drive(rig.engine.handle_user_turn(user("paris and lyon", 2, supersedes=1)))

    assert rig.history() == [
        ("assistant", "Hello."),
        ("tool", result),
        ("user", "paris and lyon"),
        ("assistant", "Both open at nine."),
    ]
    assert position(rig, "system", context_update("lookup", result)) < position(rig, "user", "paris and lyon")
    assert [c.name for c in tools.calls] == ["lookup"]
    assert rig.state.variables["hours"] == "9-5"


async def test_a_superseded_turn_keeps_an_interrupted_answer() -> None:
    tools = executor(hold=True)
    rig = engine_rig(one_node(), tools=tools)
    rig.source.reply("Hello.").reply("", calls=[call("c1", "lookup")]).reply("Sure.")
    await rig.start()
    cut = await drive_held(rig, tools, close_on_line=True, turn=user("paris", 1))

    await drive(rig.engine.handle_user_turn(user("paris please", 2, supersedes=1)))

    interrupted = '{"error":"interrupted"}'
    assert cut[-1][:2] == ("say", TOOL_FILLERS[0])
    assert rig.history() == [
        ("assistant", "Hello."),
        ("tool", interrupted),
        ("user", "paris please"),
        ("assistant", "Sure."),
    ]
    assert position(rig, "system", context_update("lookup", interrupted)) < position(
        rig, "user", "paris please"
    )


async def test_a_second_supersede_keeps_the_tool_record_once() -> None:
    tools = executor()
    rig = engine_rig(one_node(), tools=tools, fillers=False)
    rig.source.reply("Hello.").reply("", calls=[call("c1", "lookup")]).reply("One.")
    rig.source.reply("Two.").reply("Three.")
    await rig.start()
    await drive(rig.engine.handle_user_turn(user("paris", 1)))
    await drive(rig.engine.handle_user_turn(user("paris and", 2, supersedes=1)))

    await drive(rig.engine.handle_user_turn(user("paris and lyon", 3, supersedes=2)))

    assert rig.history() == [
        ("assistant", "Hello."),
        ("tool", '{"ok":"lookup"}'),
        ("user", "paris and lyon"),
        ("assistant", "Three."),
    ]


# A cut round answers ``interrupted`` only for what did not happen (Decision P08-FIX-20).

HOURS = '{"hours":"9-5"}'


def hours_executor(**over: Any) -> RecordingExecutor:
    """``lookup`` answers :data:`HOURS` and extracts ``hours``."""
    return executor(results={"lookup": HOURS}, variables={"lookup": {"hours": "9-5"}}, **over)


async def test_a_tool_that_finished_keeps_its_answer_when_the_reply_is_closed_at_a_press() -> None:
    tools = hours_executor()
    rig = engine_rig(one_node(), tools=tools, system=VOICE, fillers=False)
    rig.source.reply("", calls=[press("c1", "1"), call("c2", "lookup")])

    seen = await closed_on(rig.engine.start(), SendDtmfAction)

    assert seen[-1] == ("dtmf", "1", 250, None)
    assert tool_records(rig) == [SENT_1, ("lookup", HOURS)]
    assert rig.state.variables["hours"] == "9-5"
    assert [c.name for c in tools.calls] == ["lookup"]
    assert tools.cancelled == []


async def test_a_superseded_turn_shows_the_finished_tool_behind_a_cut_press() -> None:
    tools = hours_executor()
    rig = engine_rig(one_node(), tools=tools, system=VOICE, fillers=False)
    rig.source.reply("Hello.").reply("", calls=[press("c1", "1"), call("c2", "lookup")]).reply("Okay.")
    await rig.start()
    await closed_on(rig.engine.handle_user_turn(user("press one", 1)), SendDtmfAction)

    await drive(rig.engine.handle_user_turn(user("press one please", 2, supersedes=1)))

    assert rig.history() == [
        ("assistant", "Hello."),
        ("tool", SENT_1[1]),
        ("tool", HOURS),
        ("user", "press one please"),
        ("assistant", "Okay."),
    ]
    assert position(rig, "system", context_update("lookup", HOURS)) < position(
        rig, "user", "press one please"
    )
    assert [c.name for c in tools.calls] == ["lookup"]


async def test_actions_behind_a_cut_press_read_interrupted_and_the_call_does_not_end() -> None:
    tools = hours_executor()
    rig = engine_rig(one_node(), tools=tools, system=VOICE, fillers=False)
    ending = call("c4", "end_call", reason="done", final_message="Bye.")
    rig.source.reply("Hello.").reply(
        "", calls=[press("c1", "1"), call("c2", "lookup"), press("c3", "2"), ending]
    )
    rig.source.reply("Still here.")
    await rig.start()

    await closed_on(rig.engine.handle_user_turn(user("press one", 1)), SendDtmfAction)
    later = await rig.say("hello?")

    assert tool_records(rig) == [SENT_1, ("lookup", HOURS), ("press_button", CUT), ("end_call", CUT)]
    assert (rig.state.ended, rig.state.end_reason) == (False, None)
    assert later == [("stream", "Still here.", "t2.1", None)]


async def test_a_barge_in_on_the_filler_keeps_the_answer_of_the_tool_that_finished() -> None:
    tools = hours_executor(holding=frozenset({"track"}))
    rig = engine_rig(one_node(tools=["TL-1", "TL-2"]), tools=tools)
    rig.source.reply("", calls=[call("c1", "lookup"), call("c2", "track"), call("c3", "nope")])

    actions = await drive_held(rig, tools, close_on_line=True)

    assert actions[-1][:2] == ("say", TOOL_FILLERS[0])
    assert [name for name, _ in tools.cancelled] == ["track"]
    assert tool_records(rig) == [
        ("lookup", HOURS),
        ("track", CUT),
        ("nope", '{"error":"invalid_arguments","detail":"unknown_tool"}'),
    ]
    assert rig.state.variables["hours"] == "9-5"


UNKNOWN_TOOL = '{"error":"invalid_arguments","detail":"unknown_tool"}'


@pytest.mark.parametrize(("first", "second"), [("lookup", "track"), ("track", "lookup")])
async def test_a_reply_cancelled_while_its_tools_run_keeps_the_finished_tools_answer(
    first: str, second: str
) -> None:
    tools = hours_executor(holding=frozenset({"track"}))
    rig = engine_rig(one_node(tools=["TL-1", "TL-2"]), tools=tools, system=VOICE, fillers=False)
    ending = call("c4", "end_call", reason="done", final_message="Bye.")
    rig.source.reply("", calls=[call("c1", first), call("c2", second), call("c3", "nope"), ending])
    consumer = asyncio.create_task(drive(rig.engine.start()))
    await tools.wait_started("track")
    await tools.wait_started("lookup")

    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    answers = {"lookup": HOURS, "track": CUT}
    assert tool_records(rig) == [
        (first, answers[first]),
        (second, answers[second]),
        ("nope", UNKNOWN_TOOL),
        ("end_call", CUT),
    ]
    assert rig.state.variables["hours"] == "9-5"
    assert [name for name, _ in tools.cancelled] == ["track"]
    assert tools.active == 0
    assert (rig.state.ended, rig.state.end_reason) == (False, None)


# A system tool run that goes on after the cut reads ``in_progress`` (Decision P08-FIX-21 under specs/13 §6).


@pytest.mark.parametrize(("detaching", "content"), [(("send_sms",), IN_PROGRESS), ((), CUT)])
async def test_a_cut_system_tool_reads_in_progress_only_when_its_run_goes_on(
    detaching: tuple[str, ...], content: str
) -> None:
    tools = executor(
        names=frozenset({"send_sms"}), holding=frozenset({"send_sms"}), detaching=frozenset(detaching)
    )
    facts = dataclasses.replace(VOICE, sms_capable=True)
    rig = engine_rig(one_node(tools=[]), tools=tools, system=facts)
    rig.source.reply("Hello.").reply("", calls=[call("c1", "send_sms", message="Code 42")]).reply("Okay.")
    await rig.start()
    cut = await drive_held(rig, tools, close_on_line=True, turn=user("text me the code", 1))

    await drive(rig.engine.handle_user_turn(user("text me the code please", 2, supersedes=1)))

    assert cut[-1][:2] == ("say", TOOL_FILLERS[0])
    assert [name for name, _ in tools.cancelled] == ["send_sms"]
    assert rig.history() == [
        ("assistant", "Hello."),
        ("tool", content),
        ("user", "text me the code please"),
        ("assistant", "Okay."),
    ]
    assert position(rig, "system", context_update("send_sms", content)) < position(
        rig, "user", "text me the code please"
    )
    assert [c.name for c in tools.calls] == ["send_sms"]


# A node runs exactly the tools its refs name (Decision P08-FIX-17 item 2).


def inline(description: str, host: str) -> dict[str, Any]:
    return {"name": "lookup", "description": description, "url": f"https://{host}.hooks.test/x"}


def descriptions(rig: Rig) -> list[list[str]]:
    return [[tool.description for tool in request.tools] for request in rig.source.requests]


async def test_two_nodes_run_their_own_tool_of_one_name() -> None:
    pathway = graph(
        [
            start("a", prompt="Help.", tools=[inline("A", "a")]),
            node("b", prompt="More.", tools=[inline("B", "b")]),
        ],
        [edge("e1", "a", "b", "Caller wants more")],
    )
    tools = executor(tools=(), refs={})
    rig = engine_rig(pathway, tools=tools, fillers=False, speculative=False)
    rig.source.reply("", calls=[call("c1", "lookup")]).reply("From A.")
    rig.source.reply("", calls=[call("c2", "lookup")]).reply("From B.")
    await rig.start()
    rig.port.script("select_path", route("e1", 9))

    await rig.say("More, please.")

    assert rig.state.current_node_id == "b"
    assert descriptions(rig) == [["A"], ["A"], ["B"], ["B"]]
    assert tools.ran == [("https://a.hooks.test/x", "lookup"), ("https://b.hooks.test/x", "lookup")]
    assert tool_records(rig) == [("lookup", '{"ok":"lookup"}')] * 2


@pytest.mark.parametrize(
    ("refs", "description", "ran"),
    [
        (["TL-1", inline("inline", "a")], LOOKUP.description, "TL-1"),
        ([inline("inline", "a"), "TL-1"], "inline", "https://a.hooks.test/x"),
    ],
    ids=["stored-first", "inline-first"],
)
async def test_the_first_ref_of_a_name_is_the_tool_that_runs(
    refs: list[Any], description: str, ran: str
) -> None:
    tools = executor()
    rig = engine_rig(one_node(tools=refs), tools=tools, fillers=False)
    rig.source.reply("", calls=[call("c1", "lookup")]).reply("Done.")
    stream = json_log_stream()

    await rig.start()

    assert descriptions(rig) == [[description], [description]]
    assert tools.ran == [(ran, "lookup")]
    shadowed = [event["count"] for event in log_events(stream) if event["event"] == "engine.tool_unresolved"]
    assert shadowed == [1]


async def test_global_tools_yield_to_a_node_tool_of_the_same_name() -> None:
    pathway = one_node({"globalTools": [inline("global", "g")]}, tools=[inline("node", "n")])
    tools = executor(tools=(), refs={})
    rig = engine_rig(pathway, tools=tools, fillers=False)
    rig.source.reply("", calls=[call("c1", "lookup")]).reply("Done.")

    await rig.start()

    assert descriptions(rig)[0] == ["node"]
    assert tools.ran == [("https://n.hooks.test/x", "lookup")]


async def test_the_offer_carries_the_node_executor() -> None:
    pathway = one_node(tools=[])
    clock = engine_rig(pathway).clock
    main = pathway.nodes[0]

    bare = NodeTools(None, VOICE, clock=clock, fillers=True).offer(main, pathway)
    scoped = NodeTools(executor(), VOICE, clock=clock, fillers=True).offer(main, pathway)

    assert bare.executor is None
    assert scoped.executor is not None
    assert scoped.executor.definitions() == ()
    assert (scoped.executor.handles("lookup"), scoped.custom) == (True, frozenset())
