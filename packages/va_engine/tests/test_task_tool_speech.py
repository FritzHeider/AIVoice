"""TaskBrain tool rounds in the call pipeline: speech during tools, fillers, variables, barge-in (P08-T02b1),
the tool rounds a superseded turn hands on (specs/13 §4 Decision P08-FIX-17), and what a cut round answers
(specs/13 §4 Decision P08-FIX-20, specs/13 §6 Decision P08-FIX-21).

The harness runs on the accelerated clock: the brain, the scripted dialogue source (no socket) and the tool
executor all share it, so tool durations and the 1 200 ms filler delay are virtual time.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from functools import partial
from typing import Any

import orjson
import pytest
from dialogue_helpers import FakeSource
from pipeline_harness import Harness, harness
from task_helpers import (
    RecordingExecutor,
    assert_tool_pairing,
    drain,
    scripted_brain,
    task_context,
    user_turn,
)

from va_common.time import Clock, FrozenClock
from va_engine.models.results import PressButton, TransferRequest
from va_engine.task_runner import TaskBrain
from va_engine.task_tools import IN_PROGRESS, INTERRUPTED
from va_llm.models import ToolCall, ToolDef
from va_sim.clock import AcceleratedClock
from va_voice.phrase_cache import TOOL_FILLERS
from va_voice.pipeline import PipelineConfig
from va_voice.pipeline.models import BrainOutput, SpeakStatic, SpeakText, UserTurn

LOOKUP = ToolDef(
    name="lookup_order",
    description="Looks up an order.",
    parameters={"type": "object", "properties": {"order": {"type": "string"}}},
)
ETA = ToolDef(name="eta", parameters={"type": "object"})
SPEECH = "Let me look up order {{input.order}}."
LONG_SPEECH = "Let me look that up for you, " + "this may take a little while, " * 18 + "thanks."


def _reply(index: int) -> str:
    return f"Order {index} ships tomorrow."


def _call(index: int = 1, order: str = "A7", name: str = "lookup_order") -> ToolCall:
    return ToolCall(id=f"call_{index}", name=name, arguments_json=orjson.dumps({"order": order}).decode())


def _config() -> PipelineConfig:
    return PipelineConfig(no_input_timeout_ms=30_000)


def _brain(  # noqa: PLR0913 - test knobs, all keyword-only
    clock: Clock,
    source: FakeSource,
    executor: RecordingExecutor,
    *,
    fillers: bool = True,
    states: list[dict[str, Any]] | None = None,
    sms_capable: bool = False,
    **config: Any,
) -> TaskBrain:
    context = task_context(first_sentence="Hi, this is Maya.", **config)
    return scripted_brain(
        context,
        source,
        clock=clock,
        executor=executor,
        fillers=fillers,
        states=states,
        sms_capable=sms_capable,
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


def _id_less_round(clock: Clock, **fields: Any) -> tuple[FakeSource, RecordingExecutor]:
    """A round of two calls whose ids the provider left empty (specs/13 §4 Decision P08-FIX-16)."""
    calls = [
        ToolCall(id="", name="lookup_order", arguments_json="{}"),
        ToolCall(id="", name="eta", arguments_json="{}"),
    ]
    source = FakeSource().reply(calls=calls).reply("Done.")
    executor = RecordingExecutor(
        tools=(LOOKUP, ETA), names=frozenset({"lookup_order", "eta"}), clock=clock, **fields
    )
    return source, executor


async def test_two_calls_without_ids_get_their_own_answers() -> None:
    clock = FrozenClock()
    source, executor = _id_less_round(
        clock,
        results={"lookup_order": '{"status":"shipped"}', "eta": '{"eta":"Friday"}'},
        variables={"lookup_order": {"status": "shipped"}, "eta": {"eta": "Friday"}},
    )
    brain = _brain(clock, source, executor, fillers=False)
    await drain(brain.start())

    outputs = await drain(brain.on_user_turn(user_turn(1, "where is my order")))

    assert outputs == [("text", "Done.")]
    assert [call.name for call in executor.calls] == ["lookup_order", "eta"]
    assert [m.content for m in brain.messages if m.role == "tool"] == [
        '{"status":"shipped"}',
        '{"eta":"Friday"}',
    ]
    assert (brain.state.variables["status"], brain.state.variables["eta"]) == ("shipped", "Friday")


async def test_a_barge_in_cancels_and_answers_every_call_without_an_id() -> None:
    clock = FrozenClock()
    source, executor = _id_less_round(clock, hold=True)
    brain = _brain(clock, source, executor)
    await drain(brain.start())

    async with contextlib.aclosing(brain.on_user_turn(user_turn(1, "where is my order"))) as outputs:
        async for output in outputs:
            if isinstance(output, SpeakText):
                assert [delta async for delta in output.tokens] == []
            if isinstance(output, SpeakStatic):
                break

    assert isinstance(output, SpeakStatic)
    assert output.text in TOOL_FILLERS
    assert sorted(name for name, _ in executor.cancelled) == ["eta", "lookup_order"]
    assert executor.active == 0
    assert [m.content for m in brain.messages if m.role == "tool"] == [INTERRUPTED, INTERRUPTED]


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


# A superseded turn (specs/07 §8 pre-audio merge) keeps the tools it ran (specs/13 §4 Decision P08-FIX-17).

SHIPPED = '{"status":"shipped"}'


def _wire(source: FakeSource) -> list[dict[str, Any]]:
    """The messages of the last request as sent."""
    return [m.model_dump(mode="json", exclude_none=True) for m in source.requests[-1].messages]


def _shape(brain: TaskBrain) -> list[tuple[str, str | None, int]]:
    """Role, content and number of tool calls of every message after the opener."""
    return [(m.role, m.content, len(m.tool_calls)) for m in brain.messages[1:]]


def _history(brain: TaskBrain) -> list[tuple[str, str]]:
    return [(record.role, record.content) for record in brain.state.history[1:]]


async def _superseded_round(source: FakeSource) -> tuple[TaskBrain, RecordingExecutor]:
    """Turn 1 ran ``lookup_order`` and replied; ``source`` holds the scripts of the turns that follow."""
    clock = FrozenClock()
    executor = _executor(clock, results={"lookup_order": SHIPPED}, variables={"lookup_order": {"eta": "Fri"}})
    brain = _brain(clock, source, executor, fillers=False)
    await drain(brain.start())
    await drain(brain.on_user_turn(user_turn(1, "order A7")))
    return brain, executor


async def test_a_superseded_turn_carries_its_tool_round_behind_the_merged_message() -> None:
    source = FakeSource().reply("One moment. ", calls=[_call()]).reply(_reply(1)).reply("Both ship tomorrow.")
    brain, executor = await _superseded_round(source)

    outputs = await drain(brain.on_user_turn(user_turn(2, "order A7 and B2", supersedes=1)))

    assert outputs == [("text", "Both ship tomorrow.")]
    assert _shape(brain) == [
        ("user", "order A7 and B2", 0),
        ("assistant", None, 1),
        ("tool", SHIPPED, 0),
        ("assistant", "Both ship tomorrow.", 0),
    ]
    sent = _wire(source)
    assert [(m["role"], m.get("content")) for m in sent[-3:]] == [
        ("user", "order A7 and B2"),
        ("assistant", None),
        ("tool", SHIPPED),
    ]
    assert sent[-2]["tool_calls"][0]["id"] == sent[-1]["tool_call_id"] == "call_1"
    assert_tool_pairing([{"messages": sent}])
    assert _history(brain) == [
        ("tool", SHIPPED),
        ("user", "order A7 and B2"),
        ("assistant", "Both ship tomorrow."),
    ]
    assert [call.name for call in executor.calls] == ["lookup_order"]
    assert brain.state.variables["eta"] == "Fri"


async def test_a_second_supersede_carries_the_round_again() -> None:
    source = FakeSource().reply(calls=[_call()]).reply(_reply(1)).reply("Both.").reply("All three.")
    brain, executor = await _superseded_round(source)
    await drain(brain.on_user_turn(user_turn(2, "order A7 and B2", supersedes=1)))

    await drain(brain.on_user_turn(user_turn(3, "order A7, B2 and C3", supersedes=2)))

    assert _shape(brain) == [
        ("user", "order A7, B2 and C3", 0),
        ("assistant", None, 1),
        ("tool", SHIPPED, 0),
        ("assistant", "All three.", 0),
    ]
    assert_tool_pairing([{"messages": _wire(source)}])
    assert _history(brain) == [
        ("tool", SHIPPED),
        ("user", "order A7, B2 and C3"),
        ("assistant", "All three."),
    ]
    assert len(executor.calls) == 1


async def test_a_superseded_interrupted_round_is_carried_too() -> None:
    clock = FrozenClock()
    source = FakeSource().reply(calls=[_call()]).reply("Never mind then.")
    executor = _executor(clock, hold=True)
    brain = _brain(clock, source, executor)
    await drain(brain.start())
    async with contextlib.aclosing(brain.on_user_turn(user_turn(1, "order A7"))) as outputs:
        async for output in outputs:
            if isinstance(output, SpeakText):
                assert [delta async for delta in output.tokens] == []
            if isinstance(output, SpeakStatic):
                break

    replies = await drain(brain.on_user_turn(user_turn(2, "never mind", supersedes=1)))

    assert replies == [("text", "Never mind then.")]
    assert [name for name, _ in executor.cancelled] == ["lookup_order"]
    assert _shape(brain) == [
        ("user", "never mind", 0),
        ("assistant", None, 1),
        ("tool", INTERRUPTED, 0),
        ("assistant", "Never mind then.", 0),
    ]
    assert_tool_pairing([{"messages": _wire(source)}])
    assert _history(brain) == [
        ("tool", INTERRUPTED),
        ("user", "never mind"),
        ("assistant", "Never mind then."),
    ]


async def test_a_turn_without_tool_rounds_carries_nothing() -> None:
    clock = FrozenClock()
    source = FakeSource().reply("Which order?").reply("Got it.")
    brain = _brain(clock, source, _executor(clock))
    await drain(brain.start())
    await drain(brain.on_user_turn(user_turn(1, "my order")))

    await drain(brain.on_user_turn(user_turn(2, "my order A7", supersedes=1)))

    assert _shape(brain) == [("user", "my order A7", 0), ("assistant", "Got it.", 0)]
    assert _history(brain) == [("user", "my order A7"), ("assistant", "Got it.")]


async def _closed_on[T: BrainOutput](brain: TaskBrain, turn: UserTurn, kind: type[T]) -> T:
    """Run the reply to ``turn`` up to its first ``kind`` output and close it there (that output is
    received, nothing behind it is handed over)."""
    async with contextlib.aclosing(brain.on_user_turn(turn)) as outputs:
        async for output in outputs:
            if isinstance(output, SpeakText):
                assert [delta async for delta in output.tokens] == []
            if isinstance(output, kind):
                return output
    msg = f"no {kind.__name__} was produced"
    raise AssertionError(msg)


async def _closed_on_static(brain: TaskBrain, turn: UserTurn) -> SpeakStatic:
    """:func:`_closed_on` the first static line: the caller spoke on before the line's first audio."""
    return await _closed_on(brain, turn, SpeakStatic)


async def test_a_transfer_closed_on_its_message_is_carried_as_interrupted() -> None:
    clock = FrozenClock()
    transfer = ToolCall(id="call_t", name="transfer_call", arguments_json='{"message": "One moment."}')
    source = FakeSource().reply(calls=[_call(), transfer]).reply("Sure, what else?")
    executor = _executor(clock, results={"lookup_order": SHIPPED})
    brain = _brain(clock, source, executor, fillers=False, transfer_phone_number="+14155550133")
    await drain(brain.start())
    line = await _closed_on_static(brain, user_turn(1, "a human please"))

    replies = await drain(brain.on_user_turn(user_turn(2, "a human, or wait", supersedes=1)))

    assert (line.text, line.interruptible) == ("One moment.", False)
    assert replies == [("text", "Sure, what else?")]
    assert _shape(brain) == [
        ("user", "a human, or wait", 0),
        ("assistant", None, 2),
        ("tool", SHIPPED, 0),
        ("tool", INTERRUPTED, 0),
        ("assistant", "Sure, what else?", 0),
    ]
    assert_tool_pairing([{"messages": _wire(source)}])
    assert _history(brain) == [
        ("tool", SHIPPED),
        ("tool", INTERRUPTED),
        ("user", "a human, or wait"),
        ("assistant", "Sure, what else?"),
    ]


async def test_a_transfer_repeated_in_the_merged_turn_is_requested() -> None:
    clock = FrozenClock()
    arguments = '{"message": "One moment."}'
    transfer = ToolCall(id="call_t", name="transfer_call", arguments_json=arguments)
    again = ToolCall(id="call_u", name="transfer_call", arguments_json=arguments)
    source = FakeSource().reply(calls=[transfer]).reply(calls=[again])
    brain = _brain(clock, source, _executor(clock), fillers=False, transfer_phone_number="+14155550133")
    await drain(brain.start())
    await _closed_on_static(brain, user_turn(1, "a human please"))

    outputs = await drain(brain.on_user_turn(user_turn(2, "a human, now", supersedes=1)))

    transferring = '{"status":"transferring"}'
    assert outputs[0] == ("static", "One moment.", False)
    [(kind, request)] = outputs[1:]
    assert kind == "action"
    assert isinstance(request, TransferRequest)
    assert (request.target, request.message) == ("+14155550133", "One moment.")
    assert [m.content for m in brain.messages if m.role == "tool"] == [INTERRUPTED, transferring]
    assert _history(brain) == [
        ("tool", INTERRUPTED),
        ("user", "a human, now"),
        ("tool", transferring),
        ("assistant", "One moment."),
    ]
    sent = _wire(source)
    assert [(m["role"], m.get("content")) for m in sent[-3:]] == [
        ("user", "a human, now"),
        ("assistant", None),
        ("tool", INTERRUPTED),
    ]
    assert_tool_pairing([{"messages": sent}])


async def test_a_transfer_handed_over_keeps_its_transferring_answer() -> None:
    clock = FrozenClock()
    transfer = ToolCall(id="call_t", name="transfer_call", arguments_json='{"message": "One moment."}')
    source = FakeSource().reply(calls=[transfer])
    brain = _brain(clock, source, _executor(clock), transfer_phone_number="+14155550133")
    await drain(brain.start())

    outputs = await drain(brain.on_user_turn(user_turn(1, "a human please")))

    assert [output[0] for output in outputs] == ["static", "action"]
    assert [m.content for m in brain.messages if m.role == "tool"] == ['{"status":"transferring"}']


# A cut round answers ``interrupted`` only for what did not happen (specs/13 §4 Decision P08-FIX-20).

SENT_1 = '{"status":"sent","digits":"1"}'


def _press(index: int, digits: str) -> ToolCall:
    return ToolCall(
        id=f"call_{index}", name="press_button", arguments_json=orjson.dumps({"digits": digits}).decode()
    )


def _tool_answers(brain: TaskBrain) -> list[str | None]:
    return [m.content for m in brain.messages if m.role == "tool"]


async def test_a_tool_behind_a_cut_press_keeps_its_answer_and_is_carried() -> None:
    clock = FrozenClock()
    source = FakeSource().reply(calls=[_press(1, "1"), _call(2)]).reply("Done.")
    executor = _executor(clock, results={"lookup_order": SHIPPED}, variables={"lookup_order": {"eta": "Fri"}})
    states: list[dict[str, Any]] = []
    brain = _brain(clock, source, executor, fillers=False, states=states)
    await drain(brain.start())

    pressed = await _closed_on(brain, user_turn(1, "press one"), PressButton)

    assert pressed.digits == "1"
    assert _tool_answers(brain) == [SENT_1, SHIPPED]
    assert brain.state.variables["eta"] == "Fri"
    assert [state["eta"] for state in states] == ["Fri"]
    assert executor.cancelled == []

    replies = await drain(brain.on_user_turn(user_turn(2, "press one please", supersedes=1)))

    assert replies == [("text", "Done.")]
    assert _shape(brain) == [
        ("user", "press one please", 0),
        ("assistant", None, 2),
        ("tool", SENT_1, 0),
        ("tool", SHIPPED, 0),
        ("assistant", "Done.", 0),
    ]
    assert_tool_pairing([{"messages": _wire(source)}])
    assert len(executor.calls) == 1


async def test_actions_behind_a_cut_press_read_interrupted() -> None:
    clock = FrozenClock()
    ending = ToolCall(id="call_4", name="end_call", arguments_json='{"reason": "done"}')
    source = FakeSource().reply(calls=[_press(1, "1"), _call(2), _press(3, "2"), ending])
    executor = _executor(clock, results={"lookup_order": SHIPPED})
    brain = _brain(clock, source, executor, fillers=False)
    await drain(brain.start())

    await _closed_on(brain, user_turn(1, "press one"), PressButton)

    assert _tool_answers(brain) == [SENT_1, SHIPPED, INTERRUPTED, INTERRUPTED]
    assert brain.state.ended is False


async def test_a_barge_in_on_the_filler_keeps_the_finished_tools_answer() -> None:
    clock = FrozenClock()
    source, executor = _id_less_round(
        clock,
        holding=frozenset({"eta"}),
        results={"lookup_order": SHIPPED},
        variables={"lookup_order": {"status": "shipped"}},
    )
    states: list[dict[str, Any]] = []
    brain = _brain(clock, source, executor, states=states)
    await drain(brain.start())

    line = await _closed_on_static(brain, user_turn(1, "where is my order"))

    assert line.text in TOOL_FILLERS
    assert [name for name, _ in executor.cancelled] == ["eta"]
    assert _tool_answers(brain) == [SHIPPED, INTERRUPTED]
    assert brain.state.variables["status"] == "shipped"
    assert [state["status"] for state in states] == ["shipped"]


FRIDAY = '{"eta":"Friday"}'


@pytest.mark.parametrize(
    ("held", "answers", "merged"),
    [
        ("eta", [SHIPPED, INTERRUPTED], {"status": "shipped"}),
        ("lookup_order", [INTERRUPTED, FRIDAY], {"eta": "Friday"}),
    ],
)
async def test_a_reply_cancelled_while_its_tools_run_keeps_the_finished_tools_answer(
    held: str, answers: list[str], merged: dict[str, str]
) -> None:
    clock = FrozenClock()
    source, executor = _id_less_round(
        clock,
        holding=frozenset({held}),
        results={"lookup_order": SHIPPED, "eta": FRIDAY},
        variables={"lookup_order": {"status": "shipped"}, "eta": {"eta": "Friday"}},
    )
    states: list[dict[str, Any]] = []
    brain = _brain(clock, source, executor, fillers=False, states=states)
    await drain(brain.start())
    consumer = asyncio.create_task(drain(brain.on_user_turn(user_turn(1, "where is my order"))))
    await executor.wait_started("lookup_order")
    await executor.wait_started("eta")

    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert _tool_answers(brain) == answers
    assert [name for name, _ in executor.cancelled] == [held]
    assert executor.active == 0
    assert {name: brain.state.variables.get(name) for name in ("status", "eta")} == {
        "status": None,
        "eta": None,
    } | merged
    assert states == [merged]
    assert brain.state.ended is False


# A system tool run that goes on after the cut reads ``in_progress`` (specs/13 §6 Decision P08-FIX-21).


async def test_a_system_tool_that_runs_on_is_carried_as_in_progress() -> None:
    clock = FrozenClock()
    texting = ToolCall(id="call_1", name="send_sms", arguments_json='{"message": "Your code is 42."}')
    source = FakeSource().reply(calls=[texting]).reply("It is on its way.")
    executor = RecordingExecutor(
        tools=(LOOKUP,),
        names=frozenset({"lookup_order", "send_sms"}),
        clock=clock,
        holding=frozenset({"send_sms"}),
        detaching=frozenset({"send_sms"}),
    )
    brain = _brain(clock, source, executor, sms_capable=True)
    await drain(brain.start())

    line = await _closed_on_static(brain, user_turn(1, "text me the code"))

    assert line.text in TOOL_FILLERS
    assert [name for name, _ in executor.cancelled] == ["send_sms"]
    assert _tool_answers(brain) == [IN_PROGRESS]

    replies = await drain(brain.on_user_turn(user_turn(2, "text me the code please", supersedes=1)))

    assert replies == [("text", "It is on its way.")]
    assert _shape(brain) == [
        ("user", "text me the code please", 0),
        ("assistant", None, 1),
        ("tool", IN_PROGRESS, 0),
        ("assistant", "It is on its way.", 0),
    ]
    assert_tool_pairing([{"messages": _wire(source)}])
    assert [call.name for call in executor.calls] == ["send_sms"]


async def test_a_cut_system_tool_whose_run_stopped_is_carried_as_interrupted() -> None:
    clock = FrozenClock()
    texting = ToolCall(id="call_1", name="send_sms", arguments_json='{"message": "Your code is 42."}')
    source = FakeSource().reply(calls=[texting])
    executor = RecordingExecutor(names=frozenset({"send_sms"}), clock=clock, holding=frozenset({"send_sms"}))
    brain = _brain(clock, source, executor, sms_capable=True)
    await drain(brain.start())

    await _closed_on_static(brain, user_turn(1, "text me the code"))

    assert [name for name, _ in executor.cancelled] == ["send_sms"]
    assert _tool_answers(brain) == [INTERRUPTED]
