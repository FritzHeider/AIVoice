"""Wait for Response node handler (specs/11 §4 "Wait for Response" row, §3.2.9, Decision P07-T06 rule 6)."""

from __future__ import annotations

from typing import Any

import pytest
from graph_helpers import node
from handler_helpers import NOW_MS, handler_ctx, spoken
from service_helpers import json_log_stream, log_events

from va_engine.handlers import (
    Channel,
    HandlerResult,
    TurnContext,
    WaitForResponseHandler,
    disarm,
    handler_for,
)

HANDLER = WaitForResponseHandler()


def wait_node(config: dict[str, Any] | None = None, **data: Any) -> dict[str, Any]:
    if config is not None:
        data["waitForResponse"] = config
    return node("hold", node_type="Wait for Response", **data)


def ctx_for(config: dict[str, Any] | None = None, *, channel: Channel = "voice", **data: Any) -> TurnContext:
    return handler_ctx(wait_node(config, **data), channel=channel, extra_nodes=[node("later")])


@pytest.mark.parametrize("channel", ["voice", "widget_voice"])
async def test_on_enter_arms_the_timer_and_resets_reprompts(channel: Channel) -> None:
    ctx = ctx_for({"waitSeconds": 12}, channel=channel)
    ctx.state.wait_reprompts = 2

    result = await HANDLER.on_enter(ctx)

    assert await spoken(result.actions) == [("wait", 12)]
    assert ctx.state.wait_armed_at_ms == NOW_MS
    assert ctx.state.wait_reprompts == 0


async def test_defaults_apply_without_config() -> None:
    ctx = ctx_for()

    result = await HANDLER.on_enter(ctx)

    assert await spoken(result.actions) == [("wait", 30)]


@pytest.mark.parametrize("channel", ["sms", "widget_chat"])
async def test_text_channels_arm_ten_times_the_wait_only_for_end_call(channel: Channel) -> None:
    ending = ctx_for({"waitSeconds": 6, "onTimeout": "end_call"}, channel=channel)
    moving = ctx_for(
        {"waitSeconds": 6, "onTimeout": "move_to_node", "timeoutNodeId": "later"}, channel=channel
    )
    moving.state.wait_armed_at_ms = 5

    assert await spoken((await HANDLER.on_enter(ending)).actions) == [("wait", 60)]
    assert ending.state.wait_armed_at_ms == NOW_MS
    assert await HANDLER.on_enter(moving) == HandlerResult()
    assert moving.state.wait_armed_at_ms is None


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"text": "Take your time, {{first_name}}."}, [("say", "Take your time, Ann.", "t3.1", None)]),
        ({"prompt": "Tell them to take their time."}, [("stream", "Hi there", "t3.1", None)]),
        ({}, []),
        ({"prompt": "  "}, []),
    ],
)
async def test_respond_speaks_text_or_prompt_when_present(data: dict[str, Any], expected: list[Any]) -> None:
    ctx = handler_ctx(wait_node(**data), variables={"first_name": "Ann"})

    result = await HANDLER.respond(ctx)

    assert await spoken(result.actions) == expected


async def test_reprompts_up_to_the_maximum_then_ends_the_call() -> None:
    ctx = ctx_for({"waitSeconds": 5, "repromptText": "Still there, {{first_name}}?", "maxReprompts": 2})
    ctx.state.variables["first_name"] = "Ann"
    await HANDLER.on_enter(ctx)

    first = await HANDLER.timeout(ctx)
    second = await HANDLER.timeout(ctx)
    last = await HANDLER.timeout(ctx)

    assert await spoken(first.result.actions) == [("say", "Still there, Ann?", "t3.1", None), ("wait", 5)]
    assert await spoken(second.result.actions) == [("say", "Still there, Ann?", "t3.2", None), ("wait", 5)]
    assert await spoken(last.result.actions) == [("end", "no_input_timeout", "system", None)]
    assert (first.move_to, second.move_to, last.move_to) == (None, None, None)
    assert last.result.terminal
    assert ctx.state.wait_reprompts == 2
    assert ctx.state.ended
    assert ctx.state.end_reason == "no_input_timeout"
    assert ctx.state.wait_armed_at_ms is None


@pytest.mark.parametrize("reprompt", [None, "  ", "{{nothing}}"])
async def test_without_a_reprompt_the_timeout_applies_at_once(reprompt: str | None) -> None:
    config: dict[str, Any] = {"onTimeout": "end_call"}
    if reprompt is not None:
        config["repromptText"] = reprompt
    ctx = ctx_for(config)

    outcome = await HANDLER.timeout(ctx)

    assert await spoken(outcome.result.actions) == [("end", "no_input_timeout", "system", None)]


async def test_zero_max_reprompts_skips_the_reprompt() -> None:
    ctx = ctx_for({"repromptText": "Hello?", "maxReprompts": 0, "onTimeout": "end_call"})

    outcome = await HANDLER.timeout(ctx)

    assert await spoken(outcome.result.actions) == [("end", "no_input_timeout", "system", None)]


async def test_move_to_node_moves_after_the_reprompts() -> None:
    ctx = ctx_for(
        {"repromptText": "Hello?", "maxReprompts": 1, "onTimeout": "move_to_node", "timeoutNodeId": "later"}
    )
    await HANDLER.on_enter(ctx)

    await HANDLER.timeout(ctx)
    outcome = await HANDLER.timeout(ctx)

    assert outcome.move_to == "later"
    assert outcome.result == HandlerResult()
    assert ctx.state.wait_armed_at_ms is None
    assert not ctx.state.ended


@pytest.mark.parametrize("target", [None, "nowhere"])
async def test_move_to_a_missing_node_ends_the_call(target: str | None) -> None:
    stream = json_log_stream()
    config: dict[str, Any] = {"onTimeout": "move_to_node"}
    if target is not None:
        config["timeoutNodeId"] = target
    ctx = ctx_for(config)

    outcome = await HANDLER.timeout(ctx)

    assert outcome.move_to is None
    assert await spoken(outcome.result.actions) == [("end", "no_input_timeout", "system", None)]
    events = [
        line for line in log_events(stream) if line.get("event") == "engine.wait_timeout_target_missing"
    ]
    assert [event["node_id"] for event in events] == ["hold"]


async def test_repeat_resets_the_reprompts_and_speaks_again() -> None:
    ctx = ctx_for(
        {"waitSeconds": 8, "repromptText": "Hello?", "maxReprompts": 1, "onTimeout": "repeat"},
        text="Whenever you are ready.",
    )
    await HANDLER.on_enter(ctx)

    reprompt = await HANDLER.timeout(ctx)
    repeat = await HANDLER.timeout(ctx)
    again = await HANDLER.timeout(ctx)

    assert await spoken(reprompt.result.actions) == [("say", "Hello?", "t3.1", None), ("wait", 8)]
    assert await spoken(repeat.result.actions) == [
        ("say", "Whenever you are ready.", "t3.2", None),
        ("wait", 8),
    ]
    assert await spoken(again.result.actions) == [("say", "Hello?", "t3.3", None), ("wait", 8)]
    assert ctx.state.wait_armed_at_ms == NOW_MS
    assert not ctx.state.ended


async def test_repeat_without_text_or_prompt_only_rearms() -> None:
    ctx = ctx_for({"waitSeconds": 4, "onTimeout": "repeat"})

    outcome = await HANDLER.timeout(ctx)

    assert await spoken(outcome.result.actions) == [("wait", 4)]


@pytest.mark.parametrize("channel", ["sms", "widget_chat"])
async def test_text_channel_timeout_closes_the_conversation(channel: Channel) -> None:
    ctx = ctx_for({"repromptText": "Hello?", "maxReprompts": 3, "onTimeout": "end_call"}, channel=channel)
    await HANDLER.on_enter(ctx)

    outcome = await HANDLER.timeout(ctx)

    assert await spoken(outcome.result.actions) == [("end", "no_input_timeout", "system", None)]
    assert ctx.state.ended


async def test_disarm_clears_the_timer() -> None:
    ctx = ctx_for()
    await HANDLER.on_enter(ctx)

    disarm(ctx.state)

    assert ctx.state.wait_armed_at_ms is None


def test_always_waits_for_the_user() -> None:
    ctx = ctx_for(text="Hi.", modelOptions={"skipUserResponse": True})

    assert HANDLER.waits_for_user(ctx.node)
    assert isinstance(handler_for(ctx.node), WaitForResponseHandler)
