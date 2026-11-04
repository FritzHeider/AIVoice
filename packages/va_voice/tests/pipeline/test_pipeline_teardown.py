"""A finished CallPipeline is freed by reference counting (specs/07 §9 Decision P08-FIX-3).

A pipeline part on a reference cycle would be freed only by the cyclic collector, whose pass blocks the event
loop for every call on the pod at once. Callbacks handed to parts never capture the pipeline or the speaker,
and the end signal's tracebacks are cleared, so no pipeline part lies on a cycle after ``run()``.
"""

from __future__ import annotations

from gc_cycles import cycles_through, live_cycles_through, unreachable_after
from pipeline_fakes import ScriptedBrain, hanging
from pipeline_harness import harness

from va_voice.pipeline import EndCall, SpeakText

PIPELINE_PARTS = (
    "CallPipeline",
    "Speaker",
    "OutboundAudio",
    "FramePacer",
    "InboundPath",
    "BargeInMonitor",
    "Endpointer",
    "NoInputTimer",
    "_TurnCounter",
    "_PipelineEnded",
)
LINE = "Thanks for calling, this line is long enough to send a few frames of audio. "


async def test_an_end_call_leaves_no_pipeline_part_on_a_cycle() -> None:
    brain = ScriptedBrain(opener=[EndCall("Thanks for calling. Goodbye.")])
    async with harness(brain=brain) as h:
        result = await h.result()
        assert result.end_reason == "agent_hangup"
        assert live_cycles_through(h.pipeline, PIPELINE_PARTS) == {}


async def test_a_closed_transport_mid_utterance_leaves_no_pipeline_part_on_a_cycle() -> None:
    brain = ScriptedBrain(opener=[SpeakText(hanging(LINE))])
    async with harness(brain=brain) as h:
        await h.until(lambda: len(h.transport.frames) > 10)
        h.transport.close()
        await h.result()
        assert live_cycles_through(h.pipeline, PIPELINE_PARTS) == {}


def test_a_known_cycle_is_reported() -> None:
    class Node:
        def __init__(self) -> None:
            self.other: Node | None = None

    with unreachable_after() as garbage:
        a, b = Node(), Node()
        a.other, b.other = b, a
        del a, b
    assert cycles_through(garbage, ["Node"]) == {"Node": ["Node", "Node", "Node"]}
