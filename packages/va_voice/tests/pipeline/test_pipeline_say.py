"""CallPipeline.say: runtime lines such as the transfer-failed message (specs/07 §16 Decision P05-T13)."""

from __future__ import annotations

from pipeline_fakes import ScriptedBrain
from pipeline_harness import Harness, harness

from va_voice.pipeline import BrainAction, CallPipeline, PipelineConfig, SpeakStatic, Takeover

LINE = "I could not connect you."


def config() -> PipelineConfig:
    return PipelineConfig(no_input_timeout_ms=30_000)


def spoken(h: Harness) -> list[str]:
    return [turn.text for turn in h.observer.agent_turns]


class SayingActions:
    """An action handler that asks the pipeline to say ``LINE`` while its action still runs."""

    def __init__(self) -> None:
        self.pipeline: CallPipeline | None = None
        self.accepted: list[bool] = []

    async def handle(self, action: BrainAction) -> None:
        assert action.kind == "transfer"
        assert self.pipeline is not None
        self.accepted.append(self.pipeline.say(f"  {LINE} "))


async def test_say_speaks_the_line_as_an_agent_turn() -> None:
    brain = ScriptedBrain(opener=[SpeakStatic("Hello there.")])
    async with harness(config=config(), brain=brain) as h:
        await h.until(lambda: spoken(h) == ["Hello there."])
        assert h.pipeline.say(LINE)
        await h.until(lambda: len(spoken(h)) == 2)
        assert h.pipeline.take_over(Takeover("agent_end_call"))
        result = await h.result()
    assert spoken(h) == ["Hello there.", LINE]
    assert h.observer.agent_turns[1].interrupted is False
    assert result.end_reason == "agent_end_call"
    assert h.brain.user_turns == []


async def test_a_line_said_during_an_action_follows_the_reply() -> None:
    actions = SayingActions()
    brain = ScriptedBrain(opener=[SpeakStatic("Connecting you now."), BrainAction("transfer")])
    async with harness(config=config(), brain=brain, actions=actions) as h:
        actions.pipeline = h.pipeline
        await h.until(lambda: len(spoken(h)) == 2)
        assert h.pipeline.take_over(Takeover("agent_end_call"))
        await h.result()
    assert actions.accepted == [True]
    assert spoken(h) == ["Connecting you now.", LINE]
    first, second = h.observer.agent_turns
    assert second.started_ms >= first.ended_ms


async def test_say_refuses_blank_text_and_a_closing_pipeline() -> None:
    async with harness(config=config(), brain=ScriptedBrain(opener=[SpeakStatic("Hi.")])) as h:
        await h.until(lambda: spoken(h) == ["Hi."])
        assert not h.pipeline.say("   ")
        assert h.pipeline.take_over(Takeover("agent_end_call"))
        assert not h.pipeline.say(LINE)
        await h.result()
    assert spoken(h) == ["Hi."]
    assert not h.pipeline.say(LINE)
