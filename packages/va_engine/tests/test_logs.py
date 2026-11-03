"""Deterministic pathway_logs of the engine (specs/11 §12; Decision P07-T08c)."""

from __future__ import annotations

import asyncio

import orjson
from dialogue_helpers import NOW
from engine_helpers import Rig, condition, drive, engine_rig, first_stream, route, user
from graph_helpers import edge, fixture, graph, node, start
from handler_helpers import FakeHttp
from syrupy.assertion import SnapshotAssertion

from va_engine.logs import PathwayLogBook, dump_pathway_logs
from va_engine.models import TurnRecord, load_graph
from va_engine.ports import HttpOutcome


def restaurant() -> Rig:
    raw = fixture("restaurant_ordering")
    return engine_rig(load_graph(raw["nodes"], raw["edges"], raw["global_config"]))


async def restaurant_call() -> Rig:
    """Greeting → order (background name extraction) → unmet condition → global hours → met condition through
    the price node to the confirmation → fallback to the wait node → goodbye."""
    rig = restaurant()
    rig.source.reply("Hi, this is Ava from Bella Pasta. ").reply("spec").reply("What would you like?")
    rig.port.script("select_path", route("e_greet_order", 9, ("e_greet_manager", 2)))
    rig.port.script("extract_variables", {"first_name": "Mike"})
    await rig.start()
    rig.clock.advance(4.25)
    await rig.say("Hi, I'm Mike and I want to order")
    await rig.engine.settle_background()
    rig.clock.advance(3.5)
    rig.port.script("extract_variables", {"order_items": "large margherita", "item_count": 1})
    rig.port.script("score_condition", condition(3))
    rig.port.script("select_path", route("e_order_price", 9))
    rig.source.reply("speculative").reply("Anything else?")
    await rig.say("A large margherita please")
    rig.clock.advance(2.125)
    rig.port.script("score_condition", condition(2))
    rig.port.script("select_path", route("global:hours", 9))
    rig.source.reply("speculative").reply("We are open 11 to 10.")
    await rig.say("What are your hours?")
    rig.clock.advance(5)
    rig.port.script("score_condition", condition(9))
    rig.port.script("select_path", route("e_order_price", 8))
    rig.source.reply("speculative")
    await rig.say("That's everything")
    rig.clock.advance(1.001)
    rig.source.reply("Is there anything else I can help with?")
    await rig.say("No thanks")
    rig.clock.advance(2)
    rig.port.script("select_path", route("e_wait_goodbye", 9))
    await rig.say("Nothing else, bye")
    return rig


async def test_restaurant_call_logs_match_the_snapshot(snapshot: SnapshotAssertion) -> None:
    rig = await restaurant_call()
    assert rig.state.ended
    assert dump_pathway_logs(rig.engine.pathway_logs()).decode() == snapshot


async def test_the_same_conversation_renders_the_same_bytes() -> None:
    first, second = await restaurant_call(), await restaurant_call()
    assert dump_pathway_logs(first.engine.pathway_logs()) == dump_pathway_logs(second.engine.pathway_logs())


async def test_every_event_carries_its_turn_and_routes_their_tag_colour() -> None:
    rig = await restaurant_call()
    assert all(isinstance(data.get("turn_index"), int) for _, data in rig.events.events)
    routes = rig.events.of("call.route")
    assert [(data["to_node"], data["tag_color"]) for data in routes][:2] == [
        ("take_order", "#16A34A"),
        ("hours", "#F59E0B"),
    ]
    assert all("tag_color" in data for data in routes)
    logs = rig.engine.pathway_logs()
    assert [entry.tag.color for entry in logs][:4] == [None, "#16A34A", "#16A34A", "#F59E0B"]


async def test_webhook_node_logs_its_request_as_a_tool(snapshot: SnapshotAssertion) -> None:
    pathway = graph(
        [
            start("a", text="Hi.", modelOptions={"skipUserResponse": True}),
            node(
                "hook",
                "CRM Lookup",
                node_type="Webhook",
                webhook={
                    "url": "https://api.acme.test/lookup?key=secret",
                    "speech": "One moment.",
                    "responseData": [{"name": "status", "data": "$.status"}],
                    "responsePathways": [
                        {"variable": "status", "operator": "==", "value": "open", "targetNodeId": "open"}
                    ],
                },
            ),
            node("open", "Open", text="We are open.", tag={"name": "Open", "color": "#0EA5E9"}),
        ],
        [edge("e_hook", "a", "hook", "go", alwaysPick=True), edge("e_open", "hook", "open", "Open")],
    )
    rig = engine_rig(
        pathway,
        http=FakeHttp([HttpOutcome(kind="ok", status=200, extracted={"status": "open"}, duration_ms=42)]),
    )
    await rig.start()
    (entry,) = rig.engine.pathway_logs()
    assert entry.text == "Hi. One moment. We are open."
    assert entry.decision is not None
    assert entry.decision["tools"] == [{"name": "CRM Lookup", "status": 200, "ms": 42}]
    assert dump_pathway_logs([entry]).decode() == snapshot


def two_steps() -> Rig:
    return engine_rig(
        graph(
            [
                start(
                    "a",
                    prompt="Help the caller.",
                    extractVars=[{"name": "topic", "type": "string", "description": "Topic"}],
                    extractVarsMode="background",
                ),
                node("b", prompt="Wrap up.", tag={"name": "Wrap", "color": "#111827"}),
            ],
            [edge("e_ab", "a", "b", "Caller is done")],
        )
    )


async def test_a_barge_in_shows_what_the_caller_heard() -> None:
    rig = two_steps()
    rig.source.reply("Hello ", "there, ", "how are you?")
    turn = rig.engine.start()
    stream = await first_stream(turn)
    assert await anext(stream.tokens) == "Hello "
    await turn.aclose()
    await rig.engine.on_interrupted("Hel—")
    (entry,) = rig.engine.pathway_logs()
    assert entry.text == "Hel—"


async def test_a_superseded_turn_leaves_no_entry() -> None:
    rig = two_steps()
    rig.source.reply("Hello.")
    await rig.start()
    rig.port.script("select_path", route("e_ab", 9))
    rig.source.reply("spec").reply("Reply one.")
    await drive(rig.engine.handle_user_turn(user("I am done", 1)))
    rig.source.reply("Reply two.")
    await drive(rig.engine.handle_user_turn(user("I am done, thanks", 2, supersedes=1)))
    logs = rig.engine.pathway_logs()
    assert [(entry.text, entry.user_text) for entry in logs] == [
        ("Hello.", None),
        ("Reply two.", "I am done, thanks"),
    ]
    assert logs[1].decision is not None
    assert logs[1].decision["route"] is None
    assert logs[1].tag.color is None


async def test_a_late_background_extraction_belongs_to_the_turn_that_started_it() -> None:
    rig = two_steps()
    rig.source.reply("Hello.")
    await rig.start()
    gate = rig.port.gates["extract_variables"] = asyncio.Event()
    rig.port.script("select_path", route("e_ab", 9))
    rig.port.script("extract_variables", {"topic": "billing"})
    rig.source.reply("spec").reply("Let us wrap up.")
    await rig.say("Billing question, all done")
    rig.source.reply("spec").reply("Anything else?")
    await rig.say("Thanks")
    gate.set()
    await rig.engine.settle_background()
    assert rig.events.of("call.extraction") == [
        {"node_id": "a", "variables": {"topic": "billing"}, "turn_index": 1}
    ]
    logs = rig.engine.pathway_logs()
    assert [entry.decision["extracted"] if entry.decision else None for entry in logs] == [
        None,
        {"topic": "billing"},
        None,
    ]
    assert [entry.tag.color for entry in logs] == [None, "#111827", "#111827"]


def test_the_book_ignores_tool_records_and_events_without_a_turn() -> None:
    book = PathwayLogBook()
    record = TurnRecord(role="tool", content="ok", node_id="a", at_ms=0)
    book.on_record(1, record, node_name="A", at=NOW)
    book.on_event("call.route", {"to_node": "a"})
    book.on_event("call.sms_sent", {"turn_index": 1})
    assert book.entries(pathway_id=None, pathway_version=None) == []
    assert orjson.loads(dump_pathway_logs([])) == []
