"""Default and Global node handler (specs/11 §4 "Default"/"Global" rows, Decision P07-T06 rules 2 and 7)."""

from __future__ import annotations

import pytest
from graph_helpers import node
from handler_helpers import CHANNELS, FakeDialogue, handler_ctx, spoken
from service_helpers import json_log_stream, log_events

from va_engine.handlers import Channel, DefaultHandler, HandlerResult, handler_for

HANDLER = DefaultHandler()


@pytest.mark.parametrize("channel", CHANNELS)
async def test_static_text_is_rendered_and_said_on_every_channel(channel: Channel) -> None:
    ctx = handler_ctx(
        node("greet", text="Hi {{first_name}}, account {{account}}."),
        channel=channel,
        variables={"first_name": "Mike"},
    )

    result = await HANDLER.respond(ctx)

    assert await spoken(result.actions) == [("say", "Hi Mike, account A-7.", "t3.1", None)]
    assert result.problem is None
    assert not result.terminal
    assert ctx.dialogue.streamed == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("channel", CHANNELS)
async def test_prompt_node_streams_the_dialogue_reply(channel: Channel) -> None:
    dialogue = FakeDialogue(tokens=("Welcome ", "to ", "Bella."))
    ctx = handler_ctx(node("greet", prompt="Greet the caller."), channel=channel, dialogue=dialogue)

    result = await HANDLER.respond(ctx)

    assert await spoken(result.actions) == [("stream", "Welcome to Bella.", "t3.1", None)]
    assert dialogue.streamed == ["greet"]


async def test_blank_text_falls_back_to_the_dialogue_stream() -> None:
    ctx = handler_ctx(node("greet", text="   ", prompt="Greet."))

    result = await HANDLER.respond(ctx)

    assert await spoken(result.actions) == [("stream", "Hi there", "t3.1", None)]


async def test_text_rendering_blank_falls_back_to_the_dialogue_stream() -> None:
    ctx = handler_ctx(node("greet", text="{{missing}}", prompt="Greet."))

    result = await HANDLER.respond(ctx)

    assert await spoken(result.actions) == [("stream", "Hi there", "t3.1", None)]


async def test_template_syntax_error_is_spoken_raw_and_logged() -> None:
    stream = json_log_stream()
    ctx = handler_ctx(node("greet", text="Hi {{first_name"))

    result = await HANDLER.respond(ctx)

    assert await spoken(result.actions) == [("say", "Hi {{first_name", "t3.1", None)]
    events = [line for line in log_events(stream) if line.get("event") == "engine.template_invalid"]
    assert [(event["node_id"], event["field"]) for event in events] == [("greet", "text")]


@pytest.mark.parametrize("kind", ["text", "prompt"])
async def test_block_interruptions_makes_the_reply_not_interruptible(kind: str) -> None:
    body = {"text": "Please hold."} if kind == "text" else {"prompt": "Say hold."}
    ctx = handler_ctx(node("greet", modelOptions={"block_interruptions": True}, **body))

    result = await HANDLER.respond(ctx)

    assert [action.interruptible for action in result.actions] == [False]  # type: ignore[union-attr]


async def test_marks_count_up_within_a_turn() -> None:
    ctx = handler_ctx(node("greet", text="One."))

    first = await HANDLER.respond(ctx)
    second = await HANDLER.respond(ctx)

    assert [a.mark for a in (*first.actions, *second.actions)] == ["t3.1", "t3.2"]  # type: ignore[union-attr]


async def test_on_enter_does_nothing() -> None:
    ctx = handler_ctx(node("greet", text="Hi."))

    assert await HANDLER.on_enter(ctx) == HandlerResult()


def test_waits_for_user_unless_skip_user_response() -> None:
    waiting = handler_ctx(node("a", text="Hi.")).node
    skipping = handler_ctx(node("b", text="Hi.", modelOptions={"skipUserResponse": True})).node

    assert HANDLER.waits_for_user(waiting)
    assert not HANDLER.waits_for_user(skipping)


async def test_global_node_uses_the_default_handler() -> None:
    raw = {
        "id": "hours",
        "type": "Global",
        "data": {"name": "Hours", "globalLabel": "asks for hours", "text": "We open at {{open}}."},
    }
    ctx = handler_ctx(raw, variables={"open": "9"})

    assert ctx.node.data.is_global
    assert isinstance(handler_for(ctx.node), DefaultHandler)
    result = await handler_for(ctx.node).respond(ctx)
    assert await spoken(result.actions) == [("say", "We open at 9.", "t3.1", None)]
    assert handler_for(ctx.node).waits_for_user(ctx.node)
