"""PathwayBrain: the pathway engine behind the pipeline's Brain protocol (specs/11 §10, Decision P07-T12a)."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

import pytest
from engine_helpers import PATHWAY_ID, KeyedPort, Rig, engine_rig, route, user
from graph_helpers import edge, graph, node, start
from service_helpers import json_log_stream, log_events
from task_helpers import task_context

from va_engine.models import Action, EngineState, HumanTakeover, Say, SwitchGraph
from va_engine.models.results import PressButton, TransferRequest
from va_engine.navigation import ActiveGraph
from va_engine.pathway_brain import PATHWAY_END_REASON, PathwayBrain
from va_engine.pathway_call import pathway_facts, start_state
from va_voice.pipeline.models import BrainOutput, BrainTiming, EndCall, SpeakStatic, SpeakText, UserTurn

TARGET = "+14155550123"


def brain_of(rig: Rig) -> tuple[PathwayBrain, list[EngineState]]:
    states: list[EngineState] = []
    return PathwayBrain(rig.engine, clock=rig.clock, on_state=states.append), states


async def render(output: BrainOutput) -> tuple[Any, ...]:
    if isinstance(output, SpeakStatic):
        return ("static", output.text, output.interruptible)
    if isinstance(output, SpeakText):
        return ("text", "".join([token async for token in output.tokens]), output.interruptible)
    if isinstance(output, EndCall):
        return ("end", output.final_text, output.reason)
    if isinstance(output, TransferRequest):
        return ("transfer", output.target)
    if isinstance(output, PressButton):
        return ("dtmf", output.digits)
    return ("other", output)


async def drain(outputs: AsyncIterator[BrainOutput]) -> list[tuple[Any, ...]]:
    """Consume ``outputs`` like the pipeline: every stream is read before the next output is pulled."""
    rendered: list[tuple[Any, ...]] = []
    async with contextlib.aclosing(outputs) as running:  # type: ignore[type-var]
        async for output in running:
            rendered.append(await render(output))
    return rendered


async def test_a_static_start_node_speaks_static() -> None:
    rig = engine_rig(graph([start("a", text="Hello from Bella Pasta.")]))
    brain, states = brain_of(rig)
    assert await drain(brain.start()) == [("static", "Hello from Bella Pasta.", True)]
    assert len(states) == 1
    assert states[0] is rig.state


async def test_a_dialogue_reply_is_speak_text_and_recorded_once_read() -> None:
    rig = engine_rig(graph([start("a")]))
    rig.source.reply("Hi, ", "how can I help?")
    brain, _ = brain_of(rig)
    assert await drain(brain.start()) == [("text", "Hi, how can I help?", True)]
    assert rig.history() == [("assistant", "Hi, how can I help?")]


async def test_a_non_interruptible_line_stays_non_interruptible() -> None:
    pathway = graph(
        [
            start("a", text="Hi."),
            node("done", node_type="End Call", endCall={"speech": "Goodbye now."}),
        ],
        [edge("e", "a", "done", "Caller is done")],
    )
    rig = engine_rig(pathway)
    rig.port.script("select_path", route("e", 9))
    brain, states = brain_of(rig)
    await drain(brain.start())
    outputs = await drain(brain.on_user_turn(user("That's all", 1)))
    assert outputs == [("static", "Goodbye now.", False), ("end", None, PATHWAY_END_REASON)]
    assert rig.state.ended
    assert len(states) == 2


async def test_cold_and_warm_transfers_become_transfer_requests() -> None:
    for mode in ("cold", "warm"):
        stream = json_log_stream()
        pathway = graph(
            [
                start(
                    "t",
                    node_type="Transfer Call",
                    transfer={"mode": mode, "number": TARGET, "speech": "Connecting you."},
                )
            ]
        )
        brain, _ = brain_of(engine_rig(pathway))
        outputs = await drain(brain.start())
        assert outputs == [("static", "Connecting you.", False), ("transfer", TARGET)]
        warm_logs = [e for e in log_events(stream) if e["event"] == "pathway_brain.warm_transfer_as_cold"]
        assert len(warm_logs) == (1 if mode == "warm" else 0)


async def test_press_button_yields_press_button() -> None:
    menu = {"digits": "2w3", "speech": "Choosing billing."}
    nodes = [start("menu", node_type="Press Button", pressButton=menu), node("billing", text="Billing.")]
    pathway = graph(nodes, [edge("e_billing", "menu", "billing", "menu done", alwaysPick=True)])
    brain, _ = brain_of(engine_rig(pathway))
    outputs = await drain(brain.start())
    assert outputs == [("static", "Choosing billing.", True), ("dtmf", "2w3"), ("static", "Billing.", True)]


def wait_graph(**config: Any) -> Any:
    return graph(
        [
            start("w", node_type="Wait for Response", text="Take your time.", waitForResponse=config),
            node("next", text="Great, let's continue."),
        ],
        [edge("e", "w", "next", "Caller is ready")],
    )


async def test_wait_for_response_owns_the_no_input_timer() -> None:
    pathway = wait_graph(waitSeconds=5, repromptText="Still there?", maxReprompts=1, onTimeout="end_call")
    rig = engine_rig(pathway)
    brain, states = brain_of(rig)
    assert brain.timing().no_input_timeout_ms is None
    assert await drain(brain.start()) == [("static", "Take your time.", True)]
    assert brain.timing().no_input_timeout_ms == 5000
    assert await drain(brain.on_no_input()) == [("static", "Still there?", True)]
    assert brain.timing().no_input_timeout_ms == 5000
    outputs = await drain(brain.on_no_input())
    assert outputs[-1] == ("end", None, PATHWAY_END_REASON)
    assert brain.timing().no_input_timeout_ms is None
    assert rig.state.ended
    assert len(states) == 3


async def test_a_user_turn_ends_the_wait() -> None:
    rig = engine_rig(wait_graph(waitSeconds=3))
    rig.port.script("select_path", route("e", 9))
    brain, _ = brain_of(rig)
    await drain(brain.start())
    assert brain.timing().no_input_timeout_ms == 3000
    outputs = brain.on_user_turn(user("Ready", 1))
    assert brain.timing().no_input_timeout_ms is None
    assert await drain(outputs) == [("static", "Great, let's continue.", True)]
    assert brain.timing().no_input_timeout_ms is None
    assert rig.state.current_node_id == "next"


async def test_no_input_without_a_wait_yields_nothing() -> None:
    rig = engine_rig(graph([start("a", text="Hi.")]))
    brain, states = brain_of(rig)
    await drain(brain.start())
    assert await drain(brain.on_no_input()) == []
    assert len(states) == 1


async def test_closing_mid_stream_closes_the_engine_and_truncates() -> None:
    pathway = graph([start("a", text="Hi."), node("b")], [edge("e", "a", "b", "Caller asks")])
    rig = engine_rig(pathway)
    rig.port.script("select_path", route("e", 9))
    rig.source.reply("Hello ", "there, ", "let me explain everything.")
    brain, states = brain_of(rig)
    await drain(brain.start())
    outputs = brain.on_user_turn(user("Tell me", 1))
    first = await anext(outputs)
    assert isinstance(first, SpeakText)
    tokens = aiter(first.tokens)
    assert await anext(tokens) == "Hello "
    await outputs.aclose()
    assert len(states) == 2
    await brain.on_interrupted("Hello—")
    record = rig.state.history[-1]
    assert (record.role, record.content, record.interrupted) == ("assistant", "Hello—", True)
    assert len(states) == 3


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, BrainTiming()),
        ({"interruptibility": 3, "resumption_speed": 1}, BrainTiming(interruptibility=3, resumption_speed=1)),
        ({"interruptibility": 3, "block_interruptions": True}, BrainTiming(interruptibility=0)),
    ],
)
def test_timing_reflects_the_current_node(options: dict[str, Any], expected: BrainTiming) -> None:
    brain, _ = brain_of(engine_rig(graph([start("a", text="Hi.", modelOptions=options)])))
    assert brain.timing() == expected


def test_timing_of_a_missing_current_node_is_the_default() -> None:
    rig = engine_rig(graph([start("a", text="Hi.")]))
    rig.state.current_node_id = "gone"
    brain, _ = brain_of(rig)
    assert brain.timing() == BrainTiming()


async def test_a_failed_transfer_note_is_recorded_and_trimmed_by_a_barge_in() -> None:
    rig = engine_rig(graph([start("a", text="Hi.")]))
    brain, states = brain_of(rig)
    await drain(brain.start())
    brain.on_transfer_failed("  Sorry, nobody picked up.  ")
    assert rig.history()[-1] == ("assistant", "Sorry, nobody picked up.")
    assert len(states) == 2
    await brain.on_interrupted("Sorry, no—")
    assert rig.state.history[-1].content == "Sorry, no—"
    brain.on_transfer_failed("   ")
    assert len(rig.state.history) == 2


async def test_actions_without_a_voice_meaning_are_logged_and_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = engine_rig(graph([start("a", text="Hi.")]))

    async def actions(greeting: UserTurn | None = None) -> AsyncGenerator[Action]:
        del greeting
        yield SwitchGraph(pathway_id=str(uuid.uuid4()), version="production")
        yield Say(text="Still here.", mark="t0.1")
        yield HumanTakeover()

    monkeypatch.setattr(rig.engine, "start", actions)
    stream = json_log_stream()
    brain, _ = brain_of(rig)
    assert await drain(brain.start()) == [("static", "Still here.", True)]
    events = [e["action"] for e in log_events(stream) if e["event"] == "pathway_brain.action_ignored"]
    assert events == ["switch_graph", "human_takeover"]


async def test_aclose_gives_up_on_a_hanging_background_extraction() -> None:
    port = KeyedPort()
    gate = port.gates["extract_variables"] = asyncio.Event()
    pathway = graph(
        [
            start(
                "a",
                text="Hi.",
                extractVars=[{"name": "first_name", "type": "string", "description": "Name"}],
                extractVarsMode="background",
            )
        ]
    )
    rig = engine_rig(pathway, port=port)
    brain, states = brain_of(rig)
    await drain(brain.start())
    await drain(brain.on_user_turn(user("I'm Mike", 1)))
    await port.started.wait()
    stream = json_log_stream()
    await brain.aclose()
    gate.set()
    assert [e["event"] for e in log_events(stream) if e["event"] == "pathway_brain.settle_timeout"] == [
        "pathway_brain.settle_timeout"
    ]
    await rig.engine.settle_background()
    assert "first_name" not in rig.state.variables
    assert len(states) == 3


async def test_aclose_settles_and_closes_the_engine() -> None:
    rig = engine_rig(graph([start("a")]))
    rig.source.reply("Hi.")
    brain, states = brain_of(rig)
    await drain(brain.start())
    await brain.aclose()
    assert all(stream.closed for stream in rig.source.streams)
    assert len(states) == 2
    assert brain.variables == {}
    assert brain.pathway_logs() == rig.engine.pathway_logs()
    assert brain.engine is rig.engine
    assert brain.state is rig.state


def test_pathway_facts_and_start_state() -> None:
    context = task_context(
        request_data={"restaurant_name": "Bella"}, timezone="America/New_York", variables=None
    )
    facts = pathway_facts(context, now=context.created_at)
    assert (facts.org_id, facts.call_id, facts.channel) == (context.org_id, context.call_id, "voice")
    assert dict(facts.request_data) == {"restaurant_name": "Bella"}
    assert facts.timezone == "America/New_York"
    assert facts.builtins["call_id"] == str(context.call_id)
    assert dict(facts.transfer_list) == {}


@pytest.mark.parametrize(("start_node_id", "expected"), [(None, "a"), ("a", "a"), ("b", "b"), ("gone", "a")])
def test_start_state_honours_start_node_id(start_node_id: str | None, expected: str) -> None:
    active = ActiveGraph(PATHWAY_ID, 3, graph([start("a", text="Hi."), node("b", text="B.")]))
    context = task_context(start_node_id=start_node_id, variables={"first_name": "Mike"})
    state = start_state(active, context)
    assert state.current_node_id == expected
    assert state.visit_counts == {expected: 1}
    assert state.variables == {"first_name": "Mike"}
    assert (state.pathway_id, state.pathway_version) == (PATHWAY_ID, 3)
