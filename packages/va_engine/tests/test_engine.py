"""PathwayEngine over the restaurant template (specs/11 §5, Decision P07-T08b)."""

from __future__ import annotations

import pytest
from engine_helpers import Rig, condition, drive, engine_rig, route, user
from graph_helpers import edge, fixture, graph, node, start

from va_engine.engine import CHAIN_DEPTH_LIMIT, new_pathway_state
from va_engine.models import load_graph
from va_engine.navigation import ActiveGraph


def restaurant() -> Rig:
    raw = fixture("restaurant_ordering")
    return engine_rig(load_graph(raw["nodes"], raw["edges"], raw["global_config"]))


def order_vars(rig: Rig) -> list[str]:
    take_order = rig.engine.navigator.graph.node("take_order")
    assert take_order is not None
    return [var.name for var in take_order.data.extract_vars]


async def at_take_order(rig: Rig) -> None:
    rig.source.reply("Hi, this is Ava from Bella Pasta. ").reply("spec").reply("What would you like?")
    rig.port.script("select_path", route("e_greet_order", 9))
    rig.port.script("extract_variables", {"first_name": "Mike"})
    await rig.start()
    await rig.say("Hi, I'm Mike and I want to order")
    await rig.engine.settle_background()


async def test_start_speaks_the_start_node_and_waits() -> None:
    rig = restaurant()
    rig.source.reply("Hi, this is Ava ", "from Bella Pasta.")
    actions = await rig.start()
    assert actions == [("stream", "Hi, this is Ava from Bella Pasta.", "t0.1", None)]
    assert rig.history() == [("assistant", "Hi, this is Ava from Bella Pasta.")]
    assert rig.state.current_node_id == "greet"
    assert rig.port.calls == []
    assert rig.events.events == []
    request = rig.source.requests[0]
    assert not request.speculative
    assert "## Current step: Greeting" in (request.messages[0].content or "")


async def test_greeting_is_recorded_before_the_start_node_speaks() -> None:
    rig = restaurant()
    rig.source.reply("Hello Mike!")
    await rig.engine.start(user("Hello?", 1)).__anext__()
    assert rig.history()[0] == ("user", "Hello?")
    assert rig.state.turn_index == 1


async def test_route_with_background_extraction_and_a_fresh_reply_at_the_new_node() -> None:
    rig = restaurant()
    rig.source.reply("Hi, this is Ava.").reply("speculative").reply("What would you like to order?")
    rig.port.script("select_path", route("e_greet_order", 9, ("e_greet_manager", 2)))
    rig.port.script("extract_variables", {"first_name": "Mike"})
    await rig.start()
    actions = await rig.say("Hi, I'm Mike and I want to order")
    await rig.engine.settle_background()
    assert actions == [("stream", "What would you like to order?", "t1.1", None)]
    assert rig.state.current_node_id == "take_order"
    assert rig.state.visit_counts["take_order"] == 1
    assert rig.state.variables["first_name"] == "Mike"
    assert sorted(rig.port.names()) == ["extract_variables", "select_path"]
    route_event = rig.events.of("call.route")
    assert route_event == [
        {
            "from_node": "greet",
            "to_node": "take_order",
            "label": "Wants to place an order",
            "alternatives": [
                {"label": "Wants to speak to a manager or has a complaint", "score": 2},
                {"label": "The caller asks about opening hours or location", "score": 0},
            ],
            "global": False,
            "router_ms": 0,
        }
    ]
    assert rig.events.of("call.extraction") == [{"node_id": "greet", "variables": {"first_name": "Mike"}}]
    speculative, fresh = rig.source.streams[1:]
    assert speculative.request.speculative
    assert speculative.discarded
    assert not fresh.request.speculative
    assert "## Current step: Take Order" in (fresh.request.messages[0].content or "")
    assert rig.history()[-2:] == [
        ("user", "Hi, I'm Mike and I want to order"),
        ("assistant", "What would you like to order?"),
    ]


async def test_unmet_condition_holds_the_node_with_the_prompt_suffix() -> None:
    rig = restaurant()
    await at_take_order(rig)
    rig.events.clear()
    names = order_vars(rig)
    rig.port.script("extract_variables", dict.fromkeys(names) | {"order_items": "large margherita"})
    rig.port.script("score_condition", condition(3))
    rig.port.script("select_path", route("e_order_price", 9))
    rig.source.reply("speculative").reply("Anything else?")
    actions = await rig.say("A large margherita please")
    assert actions == [("stream", "Anything else?", "t2.1", None)]
    assert rig.state.current_node_id == "take_order"
    assert rig.events.types() == ["call.extraction", "call.loop_condition"]
    assert rig.events.of("call.loop_condition") == [
        {"node_id": "take_order", "score": 3, "met": False, "message": "Likely 3/10"}
    ]
    assert rig.state.variables["order_items"] == "large margherita"
    speculative, fresh = rig.source.streams[-2:]
    assert speculative.discarded
    assert "keep working toward it" in (fresh.request.messages[0].content or "")
    assert "keep working toward it" not in (speculative.request.messages[0].content or "")


async def test_met_condition_routes_through_a_degraded_custom_code_node_to_static_text() -> None:
    rig = restaurant()
    await at_take_order(rig)
    rig.events.clear()
    rig.port.script("score_condition", condition(9))
    rig.port.script("select_path", route("e_order_price", 8))
    rig.source.reply("speculative")
    actions = await rig.say("That's everything")
    assert len(actions) == 1
    kind, text, mark, _ = actions[0]
    assert (kind, mark) == ("say", "t2.1")
    assert text.startswith("Your total is")
    assert rig.state.current_node_id == "confirm"
    assert [data["to_node"] for data in rig.events.of("call.route")] == ["price", "confirm"]
    always = rig.events.of("call.route")[1]
    assert always["router_ms"] == 0
    assert always["label"] == "Total computed"
    assert rig.source.streams[-1].discarded


async def test_global_node_wins_and_returns_without_respeaking() -> None:
    rig = restaurant()
    await at_take_order(rig)
    rig.events.clear()
    rig.port.script("score_condition", condition(2))
    rig.port.script("select_path", route("global:hours", 9))
    rig.source.reply("speculative").reply("We are open 11 to 10.")
    actions = await rig.say("What are your hours?")
    assert actions == [("stream", "We are open 11 to 10.", "t2.1", None)]
    assert rig.state.current_node_id == "take_order"
    assert rig.state.return_stack == []
    route_event = rig.events.of("call.route")
    assert [(data["to_node"], data["global"]) for data in route_event] == [("hours", True)]
    assert rig.events.types() == ["call.extraction", "call.loop_condition", "call.route"]
    hours_request = rig.source.requests[-1]
    assert "## Current step: Opening Hours" in (hours_request.messages[0].content or "")
    assert "Take the caller's order" in (hours_request.messages[0].content or "")


async def test_fallback_to_wait_node_reprompts_then_ends_on_timeout() -> None:
    rig = restaurant()
    state = new_pathway_state(ActiveGraph(None, None, rig.engine.navigator.graph))
    state.current_node_id = "confirm"
    rig = engine_rig(rig.engine.navigator.graph, state=state)
    rig.source.reply("Is there anything else I can help with?")
    actions = await rig.say("No thanks")
    assert actions == [("wait", 20), ("stream", "Is there anything else I can help with?", "t1.1", None)]
    assert rig.state.current_node_id == "wait"
    assert rig.state.wait_armed_at_ms is not None
    assert await rig.timeout() == [("say", "Are you still there?", "t1.2", None), ("wait", 20)]
    assert await rig.timeout() == [("end", "no_input_timeout", "system", None)]
    assert rig.state.ended
    assert await rig.say("Hello?") == []
    assert await rig.timeout() == []


async def test_end_call_node_says_goodbye_and_ends() -> None:
    rig = restaurant()
    state = new_pathway_state(ActiveGraph(None, None, rig.engine.navigator.graph))
    state.current_node_id = "wait"
    rig = engine_rig(rig.engine.navigator.graph, state=state)
    rig.port.script("select_path", route("e_wait_goodbye", 9))
    actions = await rig.say("Nothing else, bye")
    assert actions == [
        ("say", "Thanks for ordering with Bella Pasta. Goodbye!", "t1.1", False),
        ("end", "agent_end_call", "agent", "t1.1"),
    ]
    assert rig.state.ended
    assert rig.state.end_reason == "agent_end_call"
    assert rig.history()[-1] == ("assistant", "Thanks for ordering with Bella Pasta. Goodbye!")


async def test_wait_timeout_move_to_node_enters_the_target() -> None:
    pathway = graph(
        [
            start(
                "w",
                node_type="Wait for Response",
                waitForResponse={"waitSeconds": 5, "onTimeout": "move_to_node", "timeoutNodeId": "next"},
            ),
            node("next", text="Let me follow up."),
        ]
    )
    rig = engine_rig(pathway)
    assert await rig.start() == [("wait", 5)]
    actions = await rig.timeout()
    assert actions == [("say", "Let me follow up.", "t0.1", None)]
    assert rig.state.current_node_id == "next"
    assert rig.events.of("call.route") == [
        {
            "from_node": "w",
            "to_node": "next",
            "label": None,
            "alternatives": [],
            "global": False,
            "router_ms": 0,
        }
    ]


async def test_wait_timeout_away_from_a_wait_node_does_nothing() -> None:
    rig = engine_rig(graph([start("a", text="Hi.")]))
    await rig.start()
    assert await rig.timeout() == []


async def test_dtmf_turns_reach_the_decisions_as_keypad_messages() -> None:
    pathway = graph(
        [start("a", text="Press 1."), node("b", text="Thanks.")],
        [edge("e", "a", "b", "Caller pressed 1")],
    )
    rig = engine_rig(pathway)
    rig.port.script("select_path", route("e", 9))
    await rig.start()
    actions = await drive(rig.engine.handle_user_turn(user("", 1, dtmf="1")))
    assert actions == [("say", "Thanks.", "t1.1", None)]
    assert rig.history()[1] == ("user", "[Caller pressed: 1]")
    assert 'Latest caller message: "[Caller pressed: 1]"' in rig.port.calls[0].user


@pytest.mark.parametrize("skip", [True, False])
async def test_skip_user_response_chains_with_an_empty_message(skip: bool) -> None:
    pathway = graph(
        [
            start("a", text="Hello.", modelOptions={"skipUserResponse": skip}),
            node("b", text="Next step."),
        ],
        [edge("e", "a", "b", "Continue")],
    )
    rig = engine_rig(pathway)
    rig.port.script("select_path", route("e", 9))
    actions = await rig.start()
    if skip:
        assert actions == [("say", "Hello.", "t0.1", None), ("say", "Next step.", "t0.2", None)]
        assert 'Latest caller message: ""' in rig.port.calls[0].user
    else:
        assert actions == [("say", "Hello.", "t0.1", None)]
        assert rig.port.calls == []
    assert CHAIN_DEPTH_LIMIT == 10
