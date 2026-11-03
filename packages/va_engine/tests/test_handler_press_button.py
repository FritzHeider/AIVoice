"""Press Button node handler (specs/11 §4 "Press Button" row, §3.2.5, Decision P07-T07)."""

from __future__ import annotations

from typing import Any

import pytest
from graph_helpers import node
from handler_helpers import handler_ctx, spoken
from service_helpers import json_log_stream, log_events

from va_engine.handlers import PressButtonHandler

HANDLER = PressButtonHandler()


def button(**config: Any) -> dict[str, Any]:
    return node("press", node_type="Press Button", pressButton={"digits": "1w2#", **config})


@pytest.mark.parametrize("channel", ["voice", "widget_voice"])
async def test_speech_then_dtmf_after_its_mark(channel: Any) -> None:
    ctx = handler_ctx(
        button(speech="Pressing {{key}} now.", durationMillis=400), channel=channel, variables={"key": "one"}
    )

    result = await HANDLER.on_enter(ctx)

    assert await spoken(result.actions) == [
        ("say", "Pressing one now.", "t3.1", None),
        ("dtmf", "1w2#", 400, "t3.1"),
    ]
    assert result.terminal is False


async def test_without_speech_dtmf_is_due_at_once() -> None:
    result = await HANDLER.on_enter(handler_ctx(button(speech="  ")))
    assert await spoken(result.actions) == [("dtmf", "1w2#", 250, None)]


@pytest.mark.parametrize("channel", ["sms", "widget_chat"])
async def test_text_channels_ignore_the_node(channel: Any) -> None:
    stream = json_log_stream()
    result = await HANDLER.on_enter(handler_ctx(button(speech="x"), channel=channel))

    assert result.actions == ()
    ignored = [event for event in log_events(stream) if event["event"] == "engine.press_button_ignored"]
    assert ignored[0]["reason"] == "text_channel"
    assert ignored[0]["level"] == "warning"


async def test_absent_config_is_ignored() -> None:
    stream = json_log_stream()
    result = await HANDLER.on_enter(handler_ctx(node("press", node_type="Press Button")))
    assert result.actions == ()
    assert [
        event["reason"] for event in log_events(stream) if event["event"] == "engine.press_button_ignored"
    ] == ["no_config"]


async def test_respond_is_empty_and_never_waits() -> None:
    ctx = handler_ctx(button())
    assert (await HANDLER.respond(ctx)).actions == ()
    assert HANDLER.waits_for_user(ctx.node) is False
