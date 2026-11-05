"""TaskBrain tool rounds in the call pipeline: speech during tools, fillers, variables, barge-in (P08-T02b1).

The harness runs on the accelerated clock: the brain, the scripted dialogue source (no socket) and the tool
executor all share it, so tool durations and the 1 200 ms filler delay are virtual time.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import partial
from typing import Any

import orjson
from dialogue_helpers import FakeSource, counter
from pipeline_harness import Harness, harness
from task_helpers import RecordingExecutor, assert_tool_pairing, drain, task_context, user_turn

from va_common.time import Clock, FrozenClock
from va_domain.org import OrgSettings
from va_engine.models.state import EngineState
from va_engine.system_tools import facts_for_call
from va_engine.task_runner import TaskBrain
from va_engine.task_tools import INTERRUPTED
from va_llm.models import ToolCall, ToolDef
from va_sim.clock import AcceleratedClock
from va_voice.phrase_cache import TOOL_FILLERS
from va_voice.pipeline import PipelineConfig

LOOKUP = ToolDef(
    name="lookup_order",
    description="Looks up an order.",
    parameters={"type": "object", "properties": {"order": {"type": "string"}}},
)
SPEECH = "Let me look up order {{input.order}}."
LONG_SPEECH = "Let me look that up for you, " + "this may take a little while, " * 18 + "thanks."


def _reply(index: int) -> str:
    return f"Order {index} ships tomorrow."


def _call(index: int = 1, order: str = "A7", name: str = "lookup_order") -> ToolCall:
    return ToolCall(id=f"call_{index}", name=name, arguments_json=orjson.dumps({"order": order}).decode())


def _config() -> PipelineConfig:
    return PipelineConfig(no_input_timeout_ms=30_000)


def _brain(
    clock: Clock,
    source: FakeSource,
    executor: RecordingExecutor,
    *,
    fillers: bool = True,
    states: list[dict[str, Any]] | None = None,
) -> TaskBrain:
    context = task_context(first_sentence="Hi, this is Maya.")
    settings = OrgSettings.model_validate({"engine": {"tool_fillers": fillers}})

    def keep(state: EngineState) -> None:
        if states is not None:
            states.append(dict(state.variables))

    return TaskBrain(
        context=context,
        settings=settings,
        gateway=source,
        counter=counter(),
        facts=facts_for_call(context, settings, sms_capable=False),
        clock=clock,
        executor=executor,
        on_state=keep,
    )


def _executor(clock: Clock, **fields: Any) -> RecordingExecutor:
    return RecordingExecutor(tools=(LOOKUP,), names=frozenset({"lookup_order"}), clock=clock, **fields)


def _sends(h: Harness) -> list[tuple[str, float]]:
    """Every text sent to TTS with its virtual ms."""
    times = [at for name, at in h.transport.calls if name == "tts.send"]
    return [(text, at) for (_, text, _), at in zip(h.tts.sent, times, strict=True)]


def _texts(h: Harness) -> Sequence[str]:
    return [text.strip() for text, _ in _sends(h)]


def _sent(h: Harness, text: str) -> bool:
    return any(text in sent for sent in _texts(h))


def _heard(h: Harness, turns: int) -> bool:
    return len(h.observer.user_turns) >= turns


async def _talk(h: Harness, turns: int) -> None:
    await h.until(lambda: len(h.observer.agent_turns) >= 1)
    for index in range(1, turns + 1):
        await h.caller.say(400)
        await h.until(partial(_heard, h, index))
        await h.until(partial(_sent, h, _reply(index)))


async def _converse(
    rounds: Sequence[Sequence[ToolCall]], *, fillers: bool = True, **executor_fields: Any
) -> tuple[Harness, RecordingExecutor, TaskBrain]:
    """One caller turn per round: the model calls the round's tools, then replies ``_reply(n)``."""
    clock = AcceleratedClock()
    source = FakeSource()
    for index, calls in enumerate(rounds, start=1):
        source.reply(calls=list(calls)).reply(_reply(index))
    executor = _executor(clock, **executor_fields)
    brain = _brain(clock, source, executor, fillers=fillers)
    utterances = [f"question number {index}" for index in range(1, len(rounds) + 1)]
    async with harness(config=_config(), utterances=utterances, clock=clock, engine=brain) as h:
        await _talk(h, len(rounds))
    return h, executor, brain


def _fillers(h: Harness) -> list[tuple[str, float]]:
    return [(text.strip(), at) for text, at in _sends(h) if text.strip() in TOOL_FILLERS]


async def test_tool_speech_starts_within_300_ms_of_the_tool_call() -> None:
    h, executor, brain = await _converse(
        [[_call(order="B12")]], speeches={"lookup_order": SPEECH}, durations={"lookup_order": 2.0}
    )
    [(_, started)] = executor.started
    speech_at = next(at for text, at in _sends(h) if text.strip() == "Let me look up order B12.")
    reply_at = next(at for text, at in _sends(h) if text.strip() == _reply(1))
    assert started <= speech_at <= started + 300
    assert reply_at >= started + 2000
    assert _texts(h) == ["Hi, this is Maya.", "Let me look up order B12.", _reply(1)]
    assert [(r.role, r.content) for r in brain.state.history if r.role == "assistant"][-2:] == [
        ("assistant", "Let me look up order B12."),
        ("assistant", _reply(1)),
    ]
    assert all(m.content != "Let me look up order B12." for m in brain.messages)


async def test_a_filler_plays_at_1200_ms_while_a_slow_tool_runs() -> None:
    h, executor, _ = await _converse([[_call()]], durations={"lookup_order": 3.0})
    [(_, started)] = executor.started
    [(text, at)] = _fillers(h)
    assert text == TOOL_FILLERS[0]
    assert started + 1200 <= at <= started + 1400


async def test_no_filler_when_the_tool_answers_quickly() -> None:
    h, _, _ = await _converse([[_call()]], durations={"lookup_order": 0.3})
    assert _texts(h) == ["Hi, this is Maya.", _reply(1)]


async def test_fillers_rotate_round_robin_across_tool_calls() -> None:
    h, _, _ = await _converse([[_call(n)] for n in range(1, 4)], durations={"lookup_order": 2.0})
    assert [text for text, _ in _fillers(h)] == list(TOOL_FILLERS)


async def test_one_filler_per_round_for_parallel_calls() -> None:
    h, executor, _ = await _converse([[_call(1), _call(2, "B8")]], durations={"lookup_order": 2.0})
    assert [text for text, _ in _fillers(h)] == [TOOL_FILLERS[0]]
    assert executor.max_active == 2


async def test_tool_fillers_off_silences_speech_and_fillers() -> None:
    h, _, _ = await _converse(
        [[_call()]], fillers=False, speeches={"lookup_order": SPEECH}, durations={"lookup_order": 3.0}
    )
    assert _texts(h) == ["Hi, this is Maya.", _reply(1)]


async def test_barge_in_during_tool_speech_cancels_the_tool() -> None:
    clock = AcceleratedClock()
    source = FakeSource().reply(calls=[_call()]).reply(_reply(1))
    executor = _executor(clock, speeches={"lookup_order": LONG_SPEECH}, durations={"lookup_order": 30.0})
    brain = _brain(clock, source, executor)
    async with harness(
        config=_config(), utterances=["where is my order", "never mind"], clock=clock, engine=brain
    ) as h:
        await h.until(lambda: len(h.observer.agent_turns) >= 1)
        await h.caller.say(400)
        await h.until(lambda: _sent(h, "Let me look that up"))
        await h.until(
            lambda: (
                h.pipeline.speaker.tracker is not None
                and h.pipeline.speaker.tracker.played_ms(h.now_ms()) > 300
            )
        )
        await h.caller.say(1000)
        await h.until(lambda: _sent(h, _reply(1)))
    assert [name for name, _ in executor.cancelled] == ["lookup_order"]
    assert h.observer.interrupts
    tool_answers = [m.content for m in brain.messages if m.role == "tool"]
    assert tool_answers == [INTERRUPTED]
    pairing = [m.model_dump(mode="json", exclude_none=True) for m in source.requests[-1].messages]
    assert_tool_pairing([{"messages": pairing}])


async def test_extracted_variables_merge_into_state_and_on_state() -> None:
    clock = FrozenClock()
    source = FakeSource().reply(calls=[_call(1), _call(2, "B8", name="eta")]).reply("Done.").reply("Again.")
    executor = RecordingExecutor(
        tools=(LOOKUP, ToolDef(name="eta", parameters={"type": "object"})),
        names=frozenset({"lookup_order", "eta"}),
        variables={"lookup_order": {"status": "shipped", "eta": "old"}, "eta": {"eta": "Friday"}},
    )
    states: list[dict[str, Any]] = []
    brain = _brain(clock, source, executor, states=states)
    await drain(brain.start())
    outputs = await drain(brain.on_user_turn(user_turn(1, "where is my order")))
    assert outputs[-1] == ("text", "Done.")
    assert brain.state.variables["status"] == "shipped"
    assert brain.state.variables["eta"] == "Friday"
    assert len(states) == 1
    assert {k: states[0][k] for k in ("status", "eta")} == {"status": "shipped", "eta": "Friday"}
    await drain(brain.on_user_turn(user_turn(2, "and now")))
    assert len(states) == 1
    assert "status" not in executor.scopes[0].variables


async def test_tool_speech_reads_the_scope_and_a_bad_template_is_skipped() -> None:
    clock = FrozenClock()
    source = FakeSource().reply(calls=[_call(order="Z9")]).reply("Done.")
    executor = _executor(clock, speeches={"lookup_order": "Checking {{input.order}} {{bad"})
    brain = _brain(clock, source, executor)
    await drain(brain.start())
    outputs = await drain(brain.on_user_turn(user_turn(1, "my order")))
    assert outputs == [("text", "Done.")]


async def test_speech_of_a_call_with_non_object_arguments_renders_without_input() -> None:
    clock = FrozenClock()
    call = ToolCall(id="call_1", name="lookup_order", arguments_json="[1]")
    source = FakeSource().reply(calls=[call]).reply("Done.")
    executor = _executor(clock, speeches={"lookup_order": "One sec{{input.order}}."})
    brain = _brain(clock, source, executor)
    await drain(brain.start())
    outputs = await drain(brain.on_user_turn(user_turn(1, "my order")))
    assert outputs == [("static", "One sec.", True), ("text", "Done.")]
