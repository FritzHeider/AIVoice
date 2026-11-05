"""Custom and system tools on pathway reply nodes (specs/13 §4/§5/§6; Decision P08-T02c1)."""

from __future__ import annotations

import contextlib
import json
from typing import Any

import pytest
from dialogue_helpers import unavailable
from engine_helpers import Rig, engine_rig, route
from graph_helpers import edge, graph, node, start
from handler_helpers import spoken
from service_helpers import json_log_stream, log_events
from task_helpers import RecordingExecutor

from va_engine import engine as engine_module
from va_engine.engine_node_tools import NO_OFFER, NodeTools
from va_engine.models import PathwayGraph, Say
from va_engine.system_tools import SystemToolFacts
from va_llm.errors import LlmConnectionError
from va_llm.models import ToolCall, ToolDef
from va_voice.phrase_cache import TOOL_FILLERS

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
        [("say", line, "t0.2", None), ("end", "agent_end_call", "agent", "t0.2")]
        if line
        else [("end", "agent_end_call", "agent", None)]
    )
    assert actions[-len(tail) :] == tail
    assert (rig.state.ended, rig.state.end_reason) == (True, "agent_end_call")
    assert tool_records(rig) == [("end_call", '{"status":"ending"}')]
    assert len(rig.source.requests) == 1


async def test_press_button_sends_dtmf_after_the_answers() -> None:
    rig = engine_rig(one_node(tools=[]), system=VOICE)
    rig.source.reply("", calls=[call("c1", "press_button", digits="1#")]).reply("Pressed.")

    actions = await rig.start()

    assert actions == [
        ("stream", "", "t0.1", None),
        ("dtmf", "1#", 250, None),
        ("stream", "Pressed.", "t0.2", None),
    ]
    assert tool_records(rig) == [("press_button", '{"status":"sent","digits":"1#"}')]


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


async def drive_held(rig: Rig, tools: RecordingExecutor, *, close_on_line: bool) -> list[tuple[Any, ...]]:
    """Start the conversation with held tools: release the waiting ones (or close the turn) at each line."""
    rendered: list[tuple[Any, ...]] = []
    async with contextlib.aclosing(rig.engine.start()) as actions:
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


async def test_a_barge_in_during_the_filler_cancels_the_tool_and_answers_interrupted() -> None:
    tools = executor(hold=True)
    rig = engine_rig(one_node(), tools=tools)
    rig.source.reply("", calls=[call("c1", "lookup"), call("c2", "nope")])

    actions = await drive_held(rig, tools, close_on_line=True)

    assert actions[-1][0] == "say"
    assert [name for name, _ in tools.cancelled] == ["lookup"]
    assert tool_records(rig) == [
        ("lookup", '{"error":"interrupted"}'),
        ("nope", '{"error":"interrupted"}'),
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
