"""End Call node handler (specs/11 §4 "End Call" row, §3.2.3, Decision P07-T06 rules 3, 4 and 7)."""

from __future__ import annotations

from typing import Any

import pytest
from graph_helpers import node
from handler_helpers import CHANNELS, FakeLines, handler_ctx, spoken, unavailable
from service_helpers import json_log_stream, log_events

from va_engine.handlers import Channel, EndCallHandler, HandlerResult, handler_for

HANDLER = EndCallHandler()


def end_node(**data: Any) -> dict[str, Any]:
    return node("bye", node_type="End Call", **data)


@pytest.mark.parametrize("channel", CHANNELS)
async def test_static_speech_then_end_after_its_mark(channel: Channel) -> None:
    ctx = handler_ctx(
        end_node(endCall={"speech": "Thanks {{first_name}}. Goodbye!"}),
        channel=channel,
        variables={"first_name": "Ann"},
    )

    result = await HANDLER.respond(ctx)

    assert await spoken(result.actions) == [
        ("say", "Thanks Ann. Goodbye!", "t3.1", False),
        ("end", "agent_end_call", "agent", "t3.1"),
    ]
    assert result.terminal
    assert ctx.state.ended
    assert ctx.state.end_reason == "agent_end_call"


async def test_prompt_mode_composes_the_line_from_the_rendered_instruction() -> None:
    lines = FakeLines(reply="  Thanks for ordering, Ann!  ")
    ctx = handler_ctx(
        end_node(
            endCall={
                "speech": "Goodbye.",
                "speechMode": "prompt",
                "speechPrompt": "Thank {{first_name}} for the order.",
            }
        ),
        variables={"first_name": "Ann"},
        lines=lines,
    )

    result = await HANDLER.respond(ctx)

    assert lines.instructions == ["Thank Ann for the order."]
    assert await spoken(result.actions) == [
        ("say", "Thanks for ordering, Ann!", "t3.1", False),
        ("end", "agent_end_call", "agent", "t3.1"),
    ]


@pytest.mark.parametrize(("reply", "reason"), [(unavailable(), "llm_unavailable"), ("   ", "blank")])
async def test_prompt_mode_falls_back_to_the_static_speech(reply: str | Exception, reason: str) -> None:
    stream = json_log_stream()
    ctx = handler_ctx(
        end_node(endCall={"speech": "Goodbye.", "speechMode": "prompt", "speechPrompt": "Say bye."}),
        lines=FakeLines(reply=reply),
    )

    result = await HANDLER.respond(ctx)

    assert await spoken(result.actions) == [
        ("say", "Goodbye.", "t3.1", False),
        ("end", "agent_end_call", "agent", "t3.1"),
    ]
    events = [line for line in log_events(stream) if line.get("event") == "engine.final_line_fallback"]
    assert [event["reason"] for event in events] == [reason]


async def test_prompt_mode_with_a_blank_rendered_instruction_uses_the_speech() -> None:
    lines = FakeLines()
    ctx = handler_ctx(
        end_node(endCall={"speech": "Bye.", "speechMode": "prompt", "speechPrompt": "{{nothing}}"}),
        lines=lines,
    )

    result = await HANDLER.respond(ctx)

    assert lines.instructions == []
    assert await spoken(result.actions) == [
        ("say", "Bye.", "t3.1", False),
        ("end", "agent_end_call", "agent", "t3.1"),
    ]


async def test_prompt_mode_with_a_blank_prompt_is_static() -> None:
    lines = FakeLines()
    ctx = handler_ctx(
        end_node(endCall={"speech": "Bye.", "speechMode": "prompt", "speechPrompt": "  "}), lines=lines
    )

    result = await HANDLER.respond(ctx)

    assert lines.instructions == []
    assert (await spoken(result.actions))[0] == ("say", "Bye.", "t3.1", False)


async def test_without_config_speech_the_node_text_is_said() -> None:
    ctx = handler_ctx(end_node(text="See you, {{first_name}}.", endCall={}), variables={"first_name": "Bo"})

    result = await HANDLER.respond(ctx)

    assert (await spoken(result.actions))[0] == ("say", "See you, Bo.", "t3.1", False)


async def test_without_speech_or_text_the_node_prompt_is_composed() -> None:
    lines = FakeLines(reply="Have a great evening!")
    ctx = handler_ctx(end_node(prompt="Wish the caller a good evening."), lines=lines)

    result = await HANDLER.respond(ctx)

    assert lines.instructions == ["Wish the caller a good evening."]
    assert (await spoken(result.actions))[0] == ("say", "Have a great evening!", "t3.1", False)


async def test_prompt_failure_without_any_fallback_ends_at_once() -> None:
    ctx = handler_ctx(end_node(prompt="Say goodbye."), lines=FakeLines(reply=unavailable()))

    result = await HANDLER.respond(ctx)

    assert await spoken(result.actions) == [("end", "agent_end_call", "agent", None)]


async def test_nothing_to_say_ends_at_once() -> None:
    ctx = handler_ctx(end_node())

    result = await HANDLER.respond(ctx)

    assert await spoken(result.actions) == [("end", "agent_end_call", "agent", None)]
    assert ctx.state.ended


async def test_on_enter_does_nothing_and_never_waits() -> None:
    ctx = handler_ctx(end_node(endCall={"speech": "Bye."}))

    assert await HANDLER.on_enter(ctx) == HandlerResult()
    assert not HANDLER.waits_for_user(ctx.node)
    assert isinstance(handler_for(ctx.node), EndCallHandler)
    assert not ctx.state.ended
