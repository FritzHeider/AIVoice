"""CallPipeline barge-in, interruption levels, concurrency rule and false barge-in (Decision P04-T12b)."""

from __future__ import annotations

import pytest
from pipeline_fakes import Said, ScriptedBrain, SlowActions, hanging, tokens
from pipeline_harness import Harness, harness
from prometheus_client import CollectorRegistry
from service_helpers import json_log_stream, log_events

from va_common.metrics import Service, build_metrics, new_registry
from va_common.time import Clock
from va_voice.latency import LatencyTracer, TurnMetrics
from va_voice.pipeline import (
    RESUME_PREFIX,
    BrainAction,
    BrainTiming,
    PipelineConfig,
    SpeakStatic,
    SpeakText,
)

WORDS = (
    "Thanks for calling the clinic today. I can help you book, move or cancel an appointment, and I can also "
    "answer questions about our opening hours, parking, insurance and the documents you should bring along. "
)
LONG = (WORDS * 3).strip()
"""About 600 characters: 3 s of fake TTS audio (5 ms per character)."""


def spoken_prefix_ok(text: str, spoken: str) -> bool:
    """``spoken`` is a whole-word prefix of ``text``."""
    return text.startswith(spoken) and (spoken in {"", text} or text[len(spoken)] == " ")


def call_names(h: Harness, *names: str) -> list[str]:
    return [name for name, _ in h.transport.calls if name in names]


def first_call(h: Harness, name: str) -> float:
    return next(t for n, t in h.transport.calls if n == name)


async def playing(h: Harness) -> None:
    """Until the agent's first frame went out."""
    await h.until(lambda: len(h.transport.frames) > 0)


def opener_brain(text: str = LONG, *, interruptible: bool = True, **timing: int | None) -> ScriptedBrain:
    return ScriptedBrain(
        opener=[SpeakStatic(text, interruptible=interruptible)],
        timing=BrainTiming(**timing) if timing else None,  # type: ignore[arg-type]
    )


def barge_metrics() -> tuple[TurnMetrics, CollectorRegistry]:
    service = build_metrics(Service.MEDIA_WORKER, new_registry(process_collectors=False))
    return TurnMetrics(service, org_tier="build"), service.registry


async def test_barge_in_sequence_order_and_timing() -> None:
    metrics, registry = barge_metrics()

    def tracer(clock: Clock) -> LatencyTracer:
        return LatencyTracer(clock, metrics=metrics)

    async with harness(
        brain=ScriptedBrain(), utterances=[Said("please stop talking now")], latency=tracer
    ) as h:
        # The LLM is still streaming while the opener plays.
        h.brain.opener.append(
            SpeakText(tokens(LONG, h.clock, per_token_delay_s=0.02, calls=h.transport.calls))
        )
        await playing(h)
        await h.clock.sleep(0.3)
        await h.caller.say(900)
        await h.until(lambda: len(h.observer.interrupts) == 1)
        record = h.observer.interrupts[0]
        assert call_names(
            h, "transport.clear", "tts.cancel", "llm.cancel", "brain.closed", "brain.interrupted"
        ) == ["transport.clear", "tts.cancel", "llm.cancel", "brain.closed", "brain.interrupted"]
        clear_ms = first_call(h, "transport.clear")
        assert record.clear_ms == pytest.approx(clear_ms, abs=1e-3)
        assert record.clear_delay_ms <= 120
        assert record.reaction_ms == pytest.approx(record.clear_ms - record.speech_start_ms)
        assert 500 <= record.reaction_ms <= 500 + 120
        assert (record.level, record.reason, record.turn_index) == (2, "speech_and_words", 0)
        assert h.transport.clears == [clear_ms]
        # Nothing of the interrupted utterance is sent after the clear.
        assert all(t <= clear_ms for t, _ in h.transport.frames)
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        agent = h.observer.agent_turns[0]
        assert agent.interrupted
        assert agent.text_spoken
        assert spoken_prefix_ok(agent.text, agent.text_spoken)
        assert agent.text_spoken != agent.text
        assert (record.played_chars, record.total_chars) == (len(agent.text_spoken), len(agent.text))
        assert h.brain.interrupted == [f"{agent.text_spoken}—"]
        assert h.brain.closed >= 1
        assert h.pipeline.stats.barge_ins == 1
        assert registry.get_sample_value("va_barge_in_total", {"honored": "true"}) == 1
        assert registry.get_sample_value("va_barge_in_reaction_ms_count") == 1
        # Endpointing continued: the interrupting speech becomes the next user turn.
        await h.until(lambda: len(h.brain.user_turns) == 1)
        assert h.brain.user_turns[0].text == "please stop talking now"


async def test_level4_any_speech_interrupts_quickly() -> None:
    async with harness(
        config=PipelineConfig(interruptibility=4), brain=opener_brain(), utterances=[Said("mm")]
    ) as h:
        await playing(h)
        await h.caller.say(400)
        await h.until(lambda: len(h.observer.interrupts) == 1)
        record = h.observer.interrupts[0]
        assert (record.level, record.reason) == (4, "any_speech")
        assert record.reaction_ms <= 150 + 120
        assert record.clear_delay_ms <= 120


async def test_backchannel_is_ignored_at_level_2() -> None:
    async with harness(brain=opener_brain(), utterances=[Said("yeah")]) as h:
        await playing(h)
        await h.clock.sleep(0.2)
        await h.caller.say(300)
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        assert h.transport.clears == []
        assert not h.observer.agent_turns[0].interrupted
        assert h.observer.interrupts == []
        await h.clock.sleep(2.0)
        assert h.brain.user_turns == []
        assert h.observer.user_turns == []
        assert h.pipeline.stats.ignored_backchannels == 1
        assert h.pipeline.stats.unhonored_barge_ins == 1
        assert h.pipeline.stats.barge_ins == 0


async def test_level2_long_speech_without_content_words_interrupts() -> None:
    async with harness(brain=opener_brain(), utterances=[Said("yeah yeah")]) as h:
        await playing(h)
        await h.caller.say(1200)
        await h.until(lambda: len(h.observer.interrupts) == 1)
        assert h.observer.interrupts[0].reason == "long_speech"
        # A barge-in turn is not a backchannel to ignore: the Brain gets it.
        await h.until(lambda: len(h.brain.user_turns) == 1)
        assert h.brain.user_turns[0].text == "yeah yeah"


@pytest.mark.parametrize(
    "config",
    [PipelineConfig(interruptibility=0), PipelineConfig(interruptibility=4, block_interruptions=True)],
    ids=["level0", "block_interruptions"],
)
async def test_blocked_speech_is_appended_to_the_next_turn(config: PipelineConfig) -> None:
    async with harness(config=config, brain=opener_brain(), utterances=[Said("wait a second")]) as h:
        await playing(h)
        await h.caller.say(1200)
        await h.until(lambda: h.pipeline.stats.held_turns == 1)
        assert h.brain.user_turns == []
        await h.until(lambda: len(h.brain.user_turns) == 1)
        assert h.transport.clears == []
        assert h.observer.interrupts == []
        agent = h.observer.agent_turns[0]
        assert not agent.interrupted
        user = h.brain.user_turns[0]
        assert user.text == "wait a second"
        assert user.supersedes is None
        assert h.observer.user_turns == [user]
        assert agent.ended_ms <= h.now_ms()


async def test_held_turns_merge_into_one() -> None:
    async with harness(
        config=PipelineConfig(interruptibility=0),
        brain=opener_brain(),
        utterances=[Said("first part"), Said("second part")],
    ) as h:
        await playing(h)
        await h.caller.say(400)
        await h.until(lambda: h.pipeline.stats.held_turns == 1)
        await h.caller.say(400)
        await h.until(lambda: h.pipeline.stats.held_turns == 2)
        await h.until(lambda: len(h.brain.user_turns) == 1)
        user = h.brain.user_turns[0]
        assert user.text == "first part second part"
        assert user.words == ("first", "part", "second", "part")
        assert user.supersedes is None
        assert h.pipeline.stats.user_turns == 1


async def test_held_turn_merges_with_a_turn_still_open_when_playback_ends() -> None:
    text = WORDS.strip()  # about 1 s of audio
    async with harness(
        config=PipelineConfig(interruptibility=0),
        brain=opener_brain(text),
        utterances=[Said("first part"), Said("second part")],
    ) as h:
        await playing(h)
        await h.caller.say(300)
        await h.until(lambda: h.pipeline.stats.held_turns == 1)
        await h.caller.say(1500)
        await h.until(lambda: len(h.brain.user_turns) == 1)
        assert h.pipeline.stats.held_turns == 1
        assert h.brain.user_turns[0].text == "first part second part"


@pytest.mark.parametrize(
    "brain",
    [opener_brain(interruptible=False), opener_brain(interruptibility=0)],
    ids=["non_interruptible_static", "node_level_0_over_call_level_4"],
)
async def test_level4_call_is_blocked_by_the_utterance_or_the_node(brain: ScriptedBrain) -> None:
    async with harness(
        config=PipelineConfig(interruptibility=4),
        brain=brain,
        utterances=[Said("hello there are you a robot")],
    ) as h:
        await playing(h)
        await h.caller.say(1200)
        await h.until(lambda: len(h.brain.user_turns) == 1)
        assert h.transport.clears == []
        assert h.observer.interrupts == []
        assert not h.observer.agent_turns[0].interrupted
        assert h.pipeline.stats.held_turns == 1


async def test_turn_before_any_audio_cancels_generation_and_concatenates() -> None:
    async with harness(
        brain=ScriptedBrain(replies=[[], [SpeakStatic("Got it.")]]),
        utterances=[Said("I want to book"), Said("an appointment for Tuesday")],
    ) as h:
        # The first reply's LLM is slow: no text, no context, no audio yet.
        h.brain.replies[0].append(
            SpeakText(tokens("Sure, when?", h.clock, first_delay_s=5.0, calls=h.transport.calls))
        )
        await h.turn(600)
        await h.turn(600)
        first, merged = h.brain.user_turns
        assert merged.text == "I want to book an appointment for Tuesday"
        assert merged.supersedes == first.index
        assert merged.start_ms == first.start_ms
        assert merged.words == (*first.words, "an", "appointment", "for", "Tuesday")
        assert h.observer.user_turns == [first, merged]
        assert h.pipeline.stats.merged_turns == 1
        assert h.transport.clears == []
        assert call_names(h, "llm.cancel") == ["llm.cancel"]
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        assert h.observer.agent_turns[0].text == "Got it."
        assert h.tts.started == [f"turn-{merged.index + 1}"]
        cancelled = next(r for r in h.observer.reports if r.latency.eot_ms is not None)
        assert all(not event.message.startswith("E2E") for event in cancelled.events)


async def test_turn_while_tts_has_not_answered_cancels_the_context_without_clear() -> None:
    async with harness(
        brain=ScriptedBrain(replies=[[SpeakStatic("Let me check that for you.")], [SpeakStatic("Okay.")]]),
        utterances=[Said("where is my order"), Said("it is order five")],
        ttfa_s=3.0,
    ) as h:
        await h.turn(600)
        await h.until(lambda: len(h.tts.started) == 1)
        await h.turn(600)
        assert h.tts.cancelled == [h.tts.started[0]]
        assert h.transport.clears == []
        assert h.brain.user_turns[1].text == "where is my order it is order five"
        assert h.observer.agent_turns == []


async def test_turn_after_reply_audio_started_is_held_not_merged() -> None:
    async with harness(
        brain=ScriptedBrain(replies=[[SpeakStatic("One moment.")], [SpeakStatic("Noted.")]]),
        utterances=[Said("check my balance"), Said("the savings account")],
    ) as h:
        # After its first line the reply still generates (slow LLM): its audio has started.
        h.brain.replies[0].append(SpeakText(tokens("Your balance is ready.", h.clock, first_delay_s=6.0)))
        await h.turn(600)
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        await h.caller.say(600)
        await h.until(lambda: h.pipeline.stats.held_turns == 1)
        assert h.pipeline.stats.merged_turns == 0
        assert len(h.brain.user_turns) == 1
        await h.until(lambda: len(h.brain.user_turns) == 2)
        assert h.brain.user_turns[1].text == "the savings account"
        assert h.brain.user_turns[1].supersedes is None
        assert [turn.text for turn in h.observer.agent_turns[:2]] == ["One moment.", "Your balance is ready."]


async def test_barge_in_during_a_turn_reply_interrupts_that_turn() -> None:
    async with harness(
        brain=ScriptedBrain(replies=[[SpeakStatic(LONG)]]),
        utterances=[Said("tell me everything"), Said("ok stop right there")],
    ) as h:
        await h.turn(600)
        await h.until(lambda: len(h.transport.frames) > 0)
        await h.caller.say(1000)
        await h.until(lambda: len(h.observer.interrupts) == 1)
        reply_index = h.brain.user_turns[0].index + 1
        assert h.observer.interrupts[0].turn_index == reply_index
        await h.until(lambda: len(h.observer.reports) == 1)
        assert h.observer.agent_turns[0].interrupted


async def test_a_segment_spanning_two_lines_interrupts_the_interruptible_one() -> None:
    brain = ScriptedBrain(
        opener=[SpeakStatic("Please hold on for a moment.", interruptible=False), SpeakStatic(LONG)]
    )
    async with harness(brain=brain, utterances=[Said("yeah yeah")], ttfa_s=0.1) as h:
        await playing(h)
        # The caller talks across the end of the non-interruptible line into the next one.
        await h.caller.say(2500)
        await h.until(lambda: len(h.observer.interrupts) == 1)
        first, second = h.observer.agent_turns[0], h.observer.interrupts[0]
        assert not first.interrupted
        assert second.turn_index != first.index
        assert second.reason == "long_speech"
        assert second.at_ms > first.ended_ms
        # The level-0 part of the segment was an unhonored attempt.
        assert h.pipeline.stats.unhonored_barge_ins == 1
        assert h.pipeline.stats.barge_ins == 1


async def turn_during_action(h: Harness, actions: SlowActions) -> None:
    """A first turn starts a reply with an action; a second turn ends while the action runs (merged)."""
    await h.turn(600)
    await h.until(lambda: len(actions.log) == 1)
    await h.caller.say(600)
    await h.until(lambda: h.pipeline.stats.merged_turns == 1)


async def test_a_turn_during_an_action_merges_without_cancelling_it() -> None:
    actions = SlowActions(3.0)
    brain = ScriptedBrain(
        replies=[[BrainAction("transfer"), SpeakStatic("Transferring you now.")], [SpeakStatic("Okay.")]]
    )
    async with harness(
        brain=brain, actions=actions, utterances=[Said("put me through to billing"), Said("actually wait")]
    ) as h:
        await turn_during_action(h, actions)
        # The generation is cancelled, the action is not: it is still running.
        assert actions.events("cancelled") == []
        assert actions.events("completed") == []
        assert len(h.brain.user_turns) == 1
        await h.until(lambda: len(h.brain.user_turns) == 2)
        (completed,) = actions.events("completed")
        assert completed <= h.now_ms()
        assert actions.events("cancelled") == []
        merged = h.brain.user_turns[1]
        assert merged.text == "put me through to billing actually wait"
        assert merged.supersedes == h.brain.user_turns[0].index
        await h.until(lambda: len(h.observer.agent_turns) == 1)
        assert [turn.text for turn in h.observer.agent_turns] == ["Okay."]


async def test_an_action_outliving_its_reply_fails_the_pipeline_when_it_fails() -> None:
    actions = SlowActions(3.0, fail=True)
    brain = ScriptedBrain(replies=[[BrainAction("send_sms")]])
    async with harness(brain=brain, actions=actions, utterances=[Said("text me"), Said("the link")]) as h:
        await turn_during_action(h, actions)
        result = await h.result()
        assert result.end_reason == "error"
        assert isinstance(result.error, RuntimeError)
        assert actions.events("failed")
        assert len(h.brain.user_turns) == 1


async def test_the_pipeline_end_cancels_an_action_in_flight() -> None:
    actions = SlowActions(30.0)
    brain = ScriptedBrain(opener=[BrainAction("hold")])
    async with harness(brain=brain, actions=actions) as h:
        stream = json_log_stream()
        await h.until(lambda: len(actions.log) == 1)
        h.transport.close()
        result = await h.result()
        assert result.end_reason == "transport_closed"
        assert len(actions.events("cancelled")) == 1
        assert any(event["event"] == "pipeline_action_abandoned" for event in log_events(stream))


async def test_opener_cancelled_before_audio_is_not_merged() -> None:
    async with harness(brain=ScriptedBrain(), utterances=[Said("hello")]) as h:
        h.brain.opener.append(SpeakText(tokens("Hi there.", h.clock, first_delay_s=5.0)))
        await h.turn(600)
        user = h.brain.user_turns[0]
        assert (user.text, user.supersedes) == ("hello", None)
        assert h.pipeline.stats.merged_turns == 1


async def test_false_barge_in_resumes_once() -> None:
    async with harness(brain=opener_brain(), utterances=[Said("")]) as h:
        await playing(h)
        await h.caller.say(1000)
        await h.until(lambda: len(h.observer.interrupts) == 1)
        interrupted = h.brain.interrupted[0].removesuffix("—")
        await h.until(lambda: len(h.tts.started) == 2)
        resume_text = "".join(text for ctx, text, _ in h.tts.sent if ctx == h.tts.started[1]).strip()
        assert resume_text == f"{RESUME_PREFIX}{LONG[len(interrupted) :].strip()}"
        assert h.pipeline.stats.false_barge_ins == 1
        assert h.brain.user_turns == []
        assert h.observer.user_turns == []
        # Noise interrupts the resume too: it is not resumed again.
        await playing(h)
        await h.until(lambda: h.transport.frames[-1][0] > first_call(h, "transport.clear") + 100)
        await h.caller.say(1000)
        await h.until(lambda: len(h.observer.interrupts) == 2)
        await h.clock.sleep(5.0)
        assert len(h.tts.started) == 2
        assert h.pipeline.stats.false_barge_ins == 1
        assert h.brain.user_turns == []
        assert len(h.brain.interrupted) == 2


async def test_false_barge_in_after_a_commit_timeout() -> None:
    async with harness(
        brain=opener_brain(), utterances=[Said("", partial="uh what")], commit_latency_s=2.0
    ) as h:
        await playing(h)
        await h.caller.say(1000)
        await h.until(lambda: len(h.tts.started) == 2)
        assert h.pipeline.stats.commit_timeouts == 1
        assert h.pipeline.stats.false_barge_ins == 1
        assert h.brain.user_turns == []
        resume = "".join(text for ctx, text, _ in h.tts.sent if ctx == h.tts.started[1])
        assert resume.startswith(RESUME_PREFIX)


async def test_a_real_turn_after_an_interrupt_cancels_the_resume() -> None:
    async with harness(brain=opener_brain(), utterances=[Said("stop I have a question")]) as h:
        await playing(h)
        await h.caller.say(1000)
        await h.until(lambda: len(h.brain.user_turns) == 1)
        await h.clock.sleep(3.0)
        assert h.brain.user_turns[0].text == "stop I have a question"
        assert h.pipeline.stats.false_barge_ins == 0
        assert len(h.tts.started) == 1


async def test_a_tts_cancel_failure_does_not_stop_the_barge_in() -> None:
    brain = ScriptedBrain(opener=[SpeakText(hanging(LONG))])  # the TTS context stays open
    async with harness(config=PipelineConfig(interruptibility=4), brain=brain) as h:

        async def broken_cancel(context_id: str) -> None:
            del context_id
            message = "socket gone"
            raise ConnectionError(message)

        h.tts.cancel = broken_cancel  # type: ignore[method-assign]
        stream = json_log_stream()
        await playing(h)
        await h.caller.say(400)
        await h.until(lambda: len(h.observer.interrupts) == 1)
        assert len(h.brain.interrupted) == 1
        assert any(event["event"] == "pipeline_tts_cancel_failed" for event in log_events(stream))


async def test_speaker_interrupt_with_nothing_playing_returns_none() -> None:
    async with harness() as h:
        await h.clock.sleep(0.1)
        assert await h.pipeline.speaker.interrupt(h.now_ms()) is None
        await h.pipeline.speaker.cancel_generation()
        assert h.pipeline.speaker.current_interruptible is None
        assert not h.pipeline.speaker.reply_audio_started
        assert h.pipeline.barge_in.partial == ""
