"""PathwayEngine with the side-effect node handlers (specs/11 §4, §5.6; Decision P07-T08b rules 3, 7-9)."""

from __future__ import annotations

import asyncio
from typing import Any

from engine_helpers import PATHWAY_ID, KeyedPort, drive, engine_rig, route
from graph_helpers import edge, graph, node, start
from handler_helpers import FakeHttp, FakeSms, ok
from navigation_helpers import P2, MemoryLoader, active

from va_engine.models import Say
from va_engine.ports import HttpCall, HttpOutcome, SmsOutcome
from va_llm.models import ToolCall

SLOT = "2026-11-04T10:00:00+00:00"


class GatedHttp:
    """Answers once ``release`` is set; ``arrived`` is set when the request came in."""

    def __init__(self, outcome: HttpOutcome) -> None:
        self.outcome = outcome
        self.arrived = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[HttpCall] = []

    async def execute(self, call: HttpCall) -> HttpOutcome:
        self.calls.append(call)
        self.arrived.set()
        await self.release.wait()
        return self.outcome


def webhook(**config: Any) -> dict[str, Any]:
    body = {"url": "https://api.acme.test/lookup", **config}
    return node("hook", node_type="Webhook", webhook=body)


async def test_webhook_speech_plays_while_the_request_runs_and_the_response_routes() -> None:
    pathways = [{"variable": "status", "operator": "==", "value": "open", "targetNodeId": "open"}]
    http = GatedHttp(HttpOutcome(kind="ok", status=200, extracted={"status": "open"}))
    pathway = graph(
        [
            start("a", text="Hi.", modelOptions={"skipUserResponse": True}),
            webhook(
                speech="One moment.",
                responseData=[{"name": "status", "data": "$.status"}],
                responsePathways=pathways,
            ),
            node("open", text="We are open."),
        ],
        [edge("e_hook", "a", "hook", "go", alwaysPick=True), edge("e_open", "hook", "open", "Open")],
    )
    rig = engine_rig(pathway, http=http)
    turn = rig.engine.start()
    assert await anext(turn) == Say(text="Hi.", mark="t0.1")
    speech = await anext(turn)
    assert speech == Say(text="One moment.", mark="t0.2")
    await http.arrived.wait()
    assert not http.release.is_set()
    http.release.set()
    rest = await drive(turn)
    assert rest == [("say", "We are open.", "t0.3", None)]
    assert rig.state.current_node_id == "open"
    assert rig.state.variables["status"] == "open"
    types = rig.events.types()
    assert types == ["call.route", "call.webhook_node", "call.route"]
    assert rig.events.of("call.route")[1] == {
        "from_node": "hook",
        "to_node": "open",
        "label": "Open",
        "alternatives": [],
        "global": False,
        "router_ms": 0,
    }
    assert rig.port.calls == []
    assert ("assistant", "One moment.") in rig.history()


async def test_closing_during_the_request_cancels_the_handler() -> None:
    http = GatedHttp(ok())
    pathway = graph(
        [start("hook", node_type="Webhook", webhook={"url": "https://a.test", "speech": "Wait."})]
    )
    rig = engine_rig(pathway, http=http)
    turn = rig.engine.start()
    assert await anext(turn) == Say(text="Wait.", mark="t0.1")
    await http.arrived.wait()
    await turn.aclose()
    assert rig.events.events == []


async def test_webhook_extraction_uses_the_latest_caller_message() -> None:
    port = KeyedPort().script("select_path", route("e_hook", 9))
    port.script("extract_variables", {"account": "A-9"})
    hook = node(
        "hook",
        node_type="Webhook",
        webhook={"url": "https://api.acme.test/a/{{account}}"},
        extractVars=[{"name": "account", "type": "string", "description": "Account id"}],
        text="Done.",
    )
    pathway = graph([start("a", text="Account?"), hook], [edge("e_hook", "a", "hook", "Gave account")])
    http = FakeHttp([ok()])
    rig = engine_rig(pathway, http=http, port=port)
    await rig.start()
    actions = await rig.say("It is A-9")
    assert actions == [("say", "Done.", "t1.1", None)]
    assert http.calls[0].url == "https://api.acme.test/a/A-9"
    assert 'Latest caller message: "It is A-9"' in port.calls[-1].user
    assert rig.events.types() == ["call.route", "call.extraction", "call.webhook_node"]


async def test_sms_failure_takes_the_fallback_edge() -> None:
    sms = {"message": "Your code", "continueOnFailure": False}
    pathway = graph(
        [
            node("sms", node_type="SMS", isStart=True, sms=sms, text="Texted you."),
            node("sorry", text="I could not text you."),
        ],
        [edge("e_fb", "sms", "sorry", "", isFallback=True)],
    )
    rig = engine_rig(pathway, sms=FakeSms(SmsOutcome(sent=False, error="rejected")))
    actions = await rig.start()
    assert actions == [("say", "I could not text you.", "t0.1", None)]
    assert rig.events.types() == ["call.sms_sent", "call.route"]


async def test_transfer_pathway_switch_and_return_on_end() -> None:
    other = graph(
        [
            start("billing", text="Billing here.", modelOptions={"skipUserResponse": True}),
            node("done", node_type="End Call", endCall={"speech": "Back you go."}),
        ],
        [edge("e_done", "billing", "done", "finished", alwaysPick=True)],
    )
    hop = {"pathwayId": str(P2), "version": 3, "returnOnEnd": True}
    pathway = graph(
        [
            start("a", text="Main.", modelOptions={"skipUserResponse": True}),
            node("hop", node_type="Transfer Pathway", transferPathway=hop),
            node("after", text="Welcome back."),
        ],
        [
            edge("e_hop", "a", "hop", "go", alwaysPick=True),
            edge("e_after", "hop", "after", "back", alwaysPick=True),
        ],
    )
    loader = MemoryLoader(
        {(str(P2), 3): active(other, P2, 3), (str(PATHWAY_ID), 1): active(pathway, PATHWAY_ID, 1)}
    )
    rig = engine_rig(pathway, loader=loader)
    actions = await rig.start()
    assert [action[1] for action in actions] == ["Main.", "Billing here.", "Welcome back."]
    assert rig.state.current_node_id == "after"
    assert rig.state.return_stack == []
    assert not rig.state.ended
    assert rig.engine.navigator.active.graph is pathway


async def test_transfer_pathway_without_loader_routes_on() -> None:
    hop = {"pathwayId": str(P2), "version": 3}
    pathway = graph(
        [start("hop", node_type="Transfer Pathway", transferPathway=hop), node("after", text="Still here.")],
        [edge("e_after", "hop", "after", "back", alwaysPick=True)],
    )
    rig = engine_rig(pathway)
    assert await rig.start() == [("say", "Still here.", "t0.1", None)]


def scheduling_graph(**config: Any) -> Any:
    body = {
        "availabilityUrl": "https://cal.acme.test/slots",
        "bookingUrl": "https://cal.acme.test/book",
        "onFailureNodeId": "sorry",
        **config,
    }
    return graph(
        [
            start("sched", node_type="Scheduling", scheduling=body, prompt="Offer the slots."),
            node("sorry", text="Booking failed."),
        ]
    )


async def test_scheduling_tool_round_books_then_the_node_responds_again() -> None:
    http = FakeHttp([ok({"slots": [{"start": SLOT}]}), ok({"booking_id": "bk-1"})])
    rig = engine_rig(scheduling_graph(), http=http)
    book = ToolCall(id="c1", name="book_slot", arguments_json=f'{{"start": "{SLOT}"}}')
    listing = ToolCall(id="c0", name="list_slots", arguments_json="{}")
    rig.source.reply("Let me book that. ", calls=[listing, book]).reply("You are booked.")
    actions = await rig.start()
    assert actions == [
        ("stream", "Let me book that. ", "t0.1", None),
        ("stream", "You are booked.", "t0.2", None),
    ]
    assert rig.state.variables["booking_id"] == "bk-1"
    first, second = rig.source.requests
    assert [tool.name for tool in first.tools] == ["list_slots", "book_slot"]
    tool_records = [record for record in rig.state.history if record.role == "tool"]
    assert [(record.tool_name, record.tool_call_id) for record in tool_records] == [
        ("list_slots", "c0"),
        ("book_slot", "c1"),
    ]
    assert "Tool book_slot returned" in str([message.content for message in second.messages])
    assert [data["purpose"] for data in rig.events.of("call.webhook_node")] == ["availability", "booking"]


async def test_scheduling_booking_failure_routes_and_skips_the_rest() -> None:
    http = FakeHttp([ok({"slots": [{"start": SLOT}]}), ok({}, status=500)])
    rig = engine_rig(scheduling_graph(), http=http)
    calls = [
        ToolCall(id="c1", name="book_slot", arguments_json=f'{{"start": "{SLOT}"}}'),
        ToolCall(id="c2", name="list_slots", arguments_json="{}"),
    ]
    rig.source.reply("Booking. ", calls=calls)
    actions = await rig.start()
    assert actions[-1] == ("say", "Booking failed.", "t0.2", None)
    assert rig.state.current_node_id == "sorry"
    contents = [record.content for record in rig.state.history if record.role == "tool"]
    assert contents[1] == '{"error":"skipped"}'


async def test_scheduling_invalid_and_unknown_calls_and_the_round_cap() -> None:
    http = FakeHttp([ok({"slots": [{"start": SLOT}]})])
    rig = engine_rig(scheduling_graph(), http=http)
    bad = [
        ToolCall(id="b1", name="book_slot", arguments_json="{not json"),
        ToolCall(id="b2", name="book_slot", arguments_json='{"start": " "}'),
        ToolCall(id="b3", name="cancel", arguments_json="{}"),
    ]
    for index in range(4):
        rig.source.reply(f"Round {index}. ", calls=bad)
    actions = await rig.start()
    assert len(actions) == 4
    assert [len(request.tools) for request in rig.source.requests] == [2, 2, 2, 0]
    contents = {record.content for record in rig.state.history if record.role == "tool"}
    assert contents == {'{"error":"invalid_arguments"}', '{"error":"unknown_tool"}'}


async def test_press_button_yields_dtmf_after_its_speech() -> None:
    pathway = graph(
        [
            start("press", node_type="Press Button", pressButton={"digits": "1#", "speech": "Pressing."}),
            node("next", text="Done."),
        ],
        [edge("e", "press", "next", "pressed", alwaysPick=True)],
    )
    rig = engine_rig(pathway)
    actions = await rig.start()
    assert actions == [
        ("say", "Pressing.", "t0.1", None),
        ("dtmf", "1#", 250, "t0.1"),
        ("say", "Done.", "t0.2", None),
    ]


async def test_knowledge_base_and_custom_code_degrade() -> None:
    pathway = graph(
        [
            start("kb", node_type="Knowledge Base", prompt="Answer from the menu.", kb="KB-1"),
            node("code", node_type="Custom Code", text="Computed."),
        ],
        [edge("e", "kb", "code", "next")],
    )
    rig = engine_rig(pathway)
    rig.source.reply("We have pasta.")
    assert await rig.start() == [("stream", "We have pasta.", "t0.1", None)]
    rig.port.script("select_path", route("e", 9))
    assert await rig.say("Thanks") == [("say", "Computed.", "t1.1", None)]


def background_graph() -> Any:
    return graph(
        [
            start(
                "a",
                text="Hi.",
                extractVars=[{"name": "first_name", "type": "string", "description": "Name"}],
                extractVarsMode="background",
            )
        ]
    )


async def test_background_extraction_is_cancelled_by_aclose() -> None:
    port = KeyedPort()
    gate = port.gates["extract_variables"] = asyncio.Event()
    pathway = background_graph()
    rig = engine_rig(pathway, port=port)
    await rig.start()
    assert await rig.say("I'm Mike") == [("say", "Hi.", "t1.1", None)]
    await port.started.wait()
    await rig.engine.aclose()
    gate.set()
    await rig.engine.settle_background()
    assert "first_name" not in rig.state.variables
    assert rig.events.of("call.extraction") == []


async def test_background_extraction_failure_is_logged() -> None:
    port = KeyedPort().script("extract_variables", RuntimeError("boom"))
    pathway = background_graph()
    rig = engine_rig(pathway, port=port)
    await rig.say("Hello")
    await rig.engine.settle_background()
    assert rig.state.variables == {}


async def test_static_scheduling_text_offers_no_tool_round() -> None:
    http = FakeHttp([ok({"slots": [{"start": SLOT}]})])
    body = {"availabilityUrl": "https://cal.acme.test/slots", "bookingUrl": "https://cal.acme.test/book"}
    pathway = graph([start("sched", node_type="Scheduling", scheduling=body, text="Pick a time.")])
    rig = engine_rig(pathway, http=http)
    assert await rig.start() == [("say", "Pick a time.", "t0.1", None)]
    assert rig.source.streams == []


async def test_extraction_without_a_caller_message_reads_the_whole_history() -> None:
    port = KeyedPort().script("extract_variables", {"account": None})
    hook = node(
        "hook",
        node_type="Webhook",
        isStart=True,
        webhook={"url": "https://api.acme.test/a"},
        extractVars=[{"name": "account", "type": "string", "description": "Account id"}],
    )
    pathway = graph([hook, node("done", text="Done.")], [edge("e", "hook", "done", "next", alwaysPick=True)])
    rig = engine_rig(pathway, http=FakeHttp([ok()]), port=port)
    assert await rig.start() == [("say", "Done.", "t0.1", None)]
    assert 'Latest caller message: ""' in port.calls[0].user
    assert rig.events.of("call.extraction")[0] == {"node_id": "hook", "variables": {}}
