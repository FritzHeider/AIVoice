"""The pathway dialogue port (specs/11 §5.7; Decision P07-T08a rule 7)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, cast

from dialogue_helpers import FakeSource, counter, dialogue_rig, unavailable
from handler_helpers import handler_ctx
from task_helpers import NOW, served, task_context

from va_common.time import FrozenClock
from va_domain.org import OrgSettings
from va_engine.dialogue import DialogueSource, GatewayDialogue
from va_engine.models import TurnRecord
from va_engine.prompt import DialogueSettings, PathwayPrompt
from va_engine.task_runner import DialogueSource as TaskDialogueSource
from va_llm.dialogue import DialogueStream
from va_llm.errors import LlmUnavailableError
from va_llm.models import ToolCall


def _ctx(**data: Any) -> Any:
    return handler_ctx({"id": "n", "type": "Default", "data": {"name": "Order", "prompt": "Ask.", **data}})


def _last(dialogue: GatewayDialogue) -> DialogueStream | None:
    return dialogue.last


def _status(dialogue: GatewayDialogue) -> tuple[LlmUnavailableError | None, str]:
    return dialogue.unavailable, dialogue.text


async def _read(tokens: AsyncIterator[str]) -> str:
    return "".join([token async for token in tokens])


async def test_stream_yields_deltas_and_keeps_the_stream_open_for_tool_calls() -> None:
    call = ToolCall(id="c1", name="lookup", arguments_json="{}")
    dialogue, source, _ = dialogue_rig(FakeSource().reply("Sure, ", "one moment.", calls=[call]))
    assert dialogue.last is None
    assert await _read(dialogue.stream(_ctx())) == "Sure, one moment."
    assert dialogue.text == "Sure, one moment."
    assert dialogue.unavailable is None
    stream = _last(dialogue)
    assert stream is not None
    assert await stream.tool_calls() == [call]
    assert source.streams[0].closed == 0
    assert source.requests[0].speculative is False
    await dialogue.aclose()
    assert source.streams[0].closed == 1


async def test_pre_token_failure_ends_empty_and_records_unavailable() -> None:
    dialogue, source, _ = dialogue_rig(FakeSource().reply(fail=unavailable()).reply("Back."))
    assert await _read(dialogue.stream(_ctx())) == ""
    assert isinstance(dialogue.unavailable, LlmUnavailableError)
    assert source.streams[0].closed == 1
    assert await _read(dialogue.stream(_ctx())) == "Back."
    assert _status(dialogue) == (None, "Back.")


async def test_closing_the_iterator_early_closes_the_stream() -> None:
    dialogue, source, _ = dialogue_rig(FakeSource().reply("one ", "two ", "three"))
    tokens = cast("AsyncGenerator[str]", dialogue.stream(_ctx()))
    async with contextlib.aclosing(tokens):
        assert await anext(tokens) == "one "
    assert source.streams[0].closed == 1
    assert dialogue.text == "one "


async def test_open_speculative_sets_the_request_flag_and_adopted_streams_are_relayed_once() -> None:
    dialogue, source, _ = dialogue_rig(FakeSource().reply("Hi ", "there.").reply("Fresh."))
    ctx = _ctx()
    stream = await dialogue.open(ctx, speculative=True)
    assert source.requests[0].speculative is True
    stream.commit()
    dialogue.adopt(stream)
    assert await _read(dialogue.stream(ctx)) == "Hi there."
    assert dialogue.last is stream
    assert len(source.streams) == 1
    assert await _read(dialogue.stream(ctx)) == "Fresh."
    assert len(source.streams) == 2


async def test_aclose_closes_an_adopted_stream_nobody_read() -> None:
    dialogue, source, _ = dialogue_rig(FakeSource().reply("x"))
    dialogue.adopt(await dialogue.open(_ctx(), speculative=True))
    await dialogue.aclose()
    assert source.streams[0].closed == 1
    assert dialogue.last is None
    assert dialogue.prompt.settings.max_tokens == 250


def test_task_runner_reexports_the_dialogue_source() -> None:
    assert TaskDialogueSource is DialogueSource


async def test_stream_over_fakellm_carries_the_current_step() -> None:
    async with served() as sim:
        await sim.rules(
            "rules:\n  - id: order\n    match: {last_user_regex: 'pizza'}\n"
            "    respond: {content: 'One pizza coming up.'}\n"
        )
        settings = DialogueSettings.for_call(task_context(), OrgSettings())
        prompt = PathwayPrompt(settings, counter=counter(), clock=FrozenClock(NOW))
        dialogue = GatewayDialogue(sim.rig.gateway, prompt)
        ctx = _ctx()
        ctx.state.add_turn(TurnRecord(role="user", content="a pizza", node_id="n", at_ms=0))
        try:
            assert await _read(dialogue.stream(ctx)) == "One pizza coming up."
        finally:
            await dialogue.aclose()
        [request] = await sim.requests()
        assert "## Current step: Order\nAsk." in request["messages"][0]["content"]
        assert request["messages"][-1] == {"role": "user", "content": "a pizza"}
