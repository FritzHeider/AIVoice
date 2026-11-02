"""TaskBrain inside the call pipeline: heard replies, end_call, actions, barge-in (P05-T09c).

The va_voice harness runs on the wall clock (FakeLLM serves real sockets, so the simulated accelerated clock
cannot be used); utterances are short.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest
from pipeline_fakes import RecordingActions
from pipeline_harness import Harness, harness
from task_helpers import Sim, assert_tool_pairing, served, task_context

from va_engine.models.results import PressButton, TransferRequest
from va_engine.task_runner import TaskBrain
from va_sim.clock import RealtimeClock
from va_voice.pipeline import PipelineConfig

SPEECH_MS = 400
BARGE_IN_MS = 1000  # past the balanced level's 900 ms speech-only threshold (specs/07 §7)
LONG_REPLY = (
    "We are open from nine in the morning until ten at night on weekdays, and from ten until six on "
    "Saturdays and Sundays, and the kitchen closes an hour before the restaurant does. On public holidays "
    "we keep the weekend hours, the bar stays open until midnight on Fridays, and brunch is served from "
    "ten until two on Sundays, with live music on the patio whenever the weather allows it."
)


@pytest.fixture
async def sim() -> AsyncIterator[Sim]:
    async with served() as running:
        yield running


def _config() -> PipelineConfig:
    return PipelineConfig(no_input_timeout_ms=30_000)


async def _say(h: Harness, users: int, speech_ms: int = SPEECH_MS) -> None:
    await h.caller.say(speech_ms)
    await h.until(lambda: len(h.observer.user_turns) >= users)


def _spoken(h: Harness) -> Sequence[str]:
    return [turn.text for turn in h.observer.agent_turns]


async def test_a_reply_is_heard(sim: Sim) -> None:
    brain = sim.brain(task_context(first_sentence="Hi, this is Maya."))
    async with harness(
        config=_config(), utterances=["what time do you open"], clock=RealtimeClock(), engine=brain
    ) as h:
        await h.until(lambda: len(h.observer.agent_turns) >= 1)
        await _say(h, 1)
        await h.until(lambda: len(h.observer.agent_turns) >= 2)
    assert [text.strip() for text in _spoken(h)] == [
        "Hi, this is Maya.",
        "Okay. You said: what time do you open.",
    ]
    assert brain.state.turn_index == 1


async def test_end_call_ends_the_pipeline_after_the_final_line(sim: Sim) -> None:
    await sim.rules(
        "rules:\n  - id: bye\n    match: {last_user_regex: 'bye'}\n"
        "    respond: {tool_calls: [{name: end_call,\n"
        "      arguments: {reason: done, final_message: 'Goodbye, have a nice evening.'}}]}\n"
    )
    brain = sim.brain(task_context(first_sentence="Hi."))
    async with harness(config=_config(), utterances=["ok bye"], clock=RealtimeClock(), engine=brain) as h:
        await h.until(lambda: len(h.observer.agent_turns) >= 1)
        await h.caller.say(SPEECH_MS)
        result = await h.result()
    assert result.end_reason == "agent_end_call"
    assert _spoken(h)[-1].strip() == "Goodbye, have a nice evening."


async def test_transfer_and_press_button_reach_the_action_handler(sim: Sim) -> None:
    await sim.rules(
        "rules:\n"
        "  - id: menu\n    match: {last_user_regex: 'menu', call_index: 0}\n"
        "    respond: {tool_calls: [{name: press_button, arguments: {digits: '3'}}]}\n"
        "  - id: agent\n    match: {last_user_regex: 'agent'}\n"
        "    respond: {tool_calls: [{name: transfer_call,\n"
        "      arguments: {label: sales, message: 'Transferring.'}}]}\n"
    )
    actions = RecordingActions()
    context = task_context(first_sentence="Hi.", transfer_list={"sales": "+14155550111"})
    brain = sim.brain(context)
    async with harness(
        config=_config(),
        utterances=["the menu", "an agent"],
        clock=RealtimeClock(),
        engine=brain,
        actions=actions,
    ) as h:
        await h.until(lambda: len(h.observer.agent_turns) >= 1)
        await _say(h, 1)
        await h.until(lambda: len(h.observer.agent_turns) >= 2)
        await _say(h, 2)
        await h.until(lambda: len(actions.handled) >= 2)
    assert actions.handled == [
        PressButton(digits="3"),
        TransferRequest(target="+14155550111", label="sales", message="Transferring."),
    ]
    assert [text.strip() for text in _spoken(h)] == ["Hi.", "Okay. You said: the menu.", "Transferring."]
    assert_tool_pairing(await sim.requests())


async def test_an_invalid_round_recovers(sim: Sim) -> None:
    await sim.rules(
        "rules:\n  - id: bad\n    match: {last_user_regex: 'menu', call_index: 0}\n"
        "    respond: {tool_calls: [{name: press_button, arguments: {digits: 'zz'}}]}\n"
    )
    brain = sim.brain(task_context(first_sentence="Hi."))
    async with harness(config=_config(), utterances=["menu"], clock=RealtimeClock(), engine=brain) as h:
        await h.until(lambda: len(h.observer.agent_turns) >= 1)
        await _say(h, 1)
        await h.until(lambda: len(h.observer.agent_turns) >= 2)
    assert _spoken(h)[-1].strip() == "Okay. You said: menu."


async def test_barge_in_keeps_only_the_heard_part_in_the_conversation(sim: Sim) -> None:
    await sim.rules(
        "rules:\n  - id: hours\n    match: {last_user_regex: 'hours'}\n"
        f"    respond: {{content: '{LONG_REPLY}'}}\n"
    )
    brain: TaskBrain = sim.brain(task_context(first_sentence="Hi."))
    async with harness(
        config=_config(), utterances=["your hours", "and parking"], clock=RealtimeClock(), engine=brain
    ) as h:
        await h.until(lambda: len(h.observer.agent_turns) >= 1)
        await _say(h, 1)
        await h.until(
            lambda: (
                h.pipeline.speaker.tracker is not None
                and h.pipeline.speaker.tracker.played_ms(h.now_ms()) > 300
            )
        )
        await _say(h, 2, BARGE_IN_MS)
        await h.until(lambda: len(h.observer.agent_turns) >= 3)
    assert h.observer.interrupts
    _, second = await sim.requests()
    heard = [m["content"] for m in second["messages"] if m["role"] == "assistant"][-1]
    assert heard.endswith("—")
    assert LONG_REPLY.startswith(heard.removesuffix("—").strip())
    assert len(heard) < len(LONG_REPLY)
    assert any(record.interrupted for record in brain.state.history)
