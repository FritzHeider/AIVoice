"""PathwayBrain inside the call pipeline: route, End Call after its line, wait timeout, barge-in (P07-T12a),
and the ``end_call`` tool's goodbye that the caller cannot talk over (P08-FIX-16).

The decisions are scripted (``KeyedPort``) and every node speaks static text, so the harness runs on the
simulators' accelerated clock.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from engine_helpers import Rig, engine_rig, route
from graph_helpers import edge, graph, node, start
from pipeline_harness import Harness, harness

from va_engine.pathway_brain import PATHWAY_END_REASON, PathwayBrain
from va_engine.system_tools import SystemToolFacts
from va_llm.models import ToolCall
from va_voice.pipeline import PipelineConfig

CONFIG = PipelineConfig(no_input_timeout_ms=30_000)
WAIT_S = 1
WINDOW_MS = 40.0
LONG_LINE = (
    "Welcome to Bella Pasta, where every dish is made fresh to order from family recipes that have been "
    "handed down for four generations, and where our wood fired oven has been burning every single day "
    "since the restaurant first opened its doors on the corner of Main Street almost fifty years ago."
)


def brain_of(rig: Rig) -> PathwayBrain:
    return PathwayBrain(rig.engine, clock=rig.clock)


def spoken(h: Harness) -> Sequence[str]:
    return [turn.text for turn in h.observer.agent_turns]


async def test_route_to_end_call_ends_after_the_final_line() -> None:
    pathway = graph(
        [
            start("greet", text="Hi, welcome to Bella Pasta."),
            node("bye", node_type="End Call", endCall={"speech": "Thanks for calling, goodbye."}),
        ],
        [edge("e_bye", "greet", "bye", "Caller says goodbye")],
    )
    rig = engine_rig(pathway)
    rig.port.script("select_path", route("e_bye", 9))
    async with harness(config=CONFIG, utterances=["that's all bye"], engine=brain_of(rig)) as h:
        await h.until(lambda: len(h.observer.agent_turns) >= 1)
        await h.caller.say(600)
        result = await h.result()
    assert result.end_reason == PATHWAY_END_REASON
    assert spoken(h) == ["Hi, welcome to Bella Pasta.", "Thanks for calling, goodbye."]
    final = h.observer.agent_turns[-1]
    assert (final.text_spoken, final.interrupted) == (final.text, False)
    assert [record.content for record in rig.state.history] == [
        "Hi, welcome to Bella Pasta.",
        "that's all bye",
        "Thanks for calling, goodbye.",
    ]
    assert rig.events.of("call.route")[0]["to_node"] == "bye"


async def test_a_wait_node_reprompts_then_ends_on_its_own_timer() -> None:
    pathway = graph(
        [
            start(
                "w",
                node_type="Wait for Response",
                text="Take your time.",
                waitForResponse={
                    "waitSeconds": WAIT_S,
                    "repromptText": "Are you still there?",
                    "maxReprompts": 1,
                    "onTimeout": "end_call",
                },
            )
        ]
    )
    rig = engine_rig(pathway)
    async with harness(config=CONFIG, engine=brain_of(rig)) as h:
        result = await h.result()
    assert result.end_reason == PATHWAY_END_REASON
    assert spoken(h) == ["Take your time.", "Are you still there?"]
    assert result.stats.no_input_reprompts == 0
    opener, reprompt = h.observer.agent_turns
    wait_ms = WAIT_S * 1000
    assert wait_ms <= reprompt.started_ms - opener.ended_ms <= wait_ms + WINDOW_MS + 100
    assert (rig.state.ended, rig.state.end_reason) == (True, "no_input_timeout")


async def test_a_barge_in_truncates_the_engine_history() -> None:
    pathway = graph(
        [start("a", text=LONG_LINE), node("b", text="Of course, go ahead.")],
        [edge("e", "a", "b", "Caller wants to interrupt")],
    )
    rig = engine_rig(pathway)
    rig.port.script("select_path", route("e", 9))
    async with harness(config=CONFIG, utterances=["wait a second please"], engine=brain_of(rig)) as h:
        await h.until(lambda: h.pipeline.speaker.is_playing(h.now_ms()))
        await h.caller.say(1000)
        await h.until(lambda: len(h.observer.interrupts) == 1)
        await h.until(lambda: len(h.observer.agent_turns) >= 2)
    first = rig.state.history[0]
    assert first.interrupted
    assert first.content.endswith("—")
    assert len(first.content) < len(LONG_LINE)
    assert first.content == h.observer.agent_turns[0].text_spoken + "—"
    assert [record.content for record in rig.state.history[1:]] == [
        "wait a second please",
        "Of course, go ahead.",
    ]


async def test_speaking_over_the_end_call_goodbye_still_ends_the_call() -> None:
    pathway = graph([start("a", prompt="Help the caller.")], [], {"allowEndCallTool": True})
    rig = engine_rig(pathway, system=SystemToolFacts(runner="pathway", channel="voice"))
    arguments = json.dumps({"reason": "caller is done", "final_message": LONG_LINE})
    ending = ToolCall(id="", name="end_call", arguments_json=arguments)
    rig.source.reply("Hi, welcome to Bella Pasta.").reply("", calls=[ending])
    utterances = ["that's all bye", "thanks so much bye bye"]
    async with harness(config=CONFIG, utterances=utterances, engine=brain_of(rig)) as h:
        await h.until(lambda: len(h.observer.agent_turns) >= 1)
        await h.caller.say(600)
        await h.until(lambda: len(h.tts.sent) > 1 and h.pipeline.speaker.is_playing(h.now_ms()))
        await h.caller.say(1500)
        result = await h.result()
    assert result.end_reason == PATHWAY_END_REASON
    final = h.observer.agent_turns[-1]
    assert (final.text, final.text_spoken, final.interrupted) == (LONG_LINE, LONG_LINE, False)
    assert h.observer.interrupts == []
    assert "transport.clear" not in [name for name, _ in h.transport.calls]
    assert (rig.state.ended, rig.state.end_reason) == (True, "agent_end_call")
    assert [record.content for record in rig.state.history if record.role == "assistant"] == [
        "Hi, welcome to Bella Pasta.",
        LONG_LINE,
    ]
