"""Transfer Call node handler (specs/11 §4 "Transfer Call" row, §3.2.2, Decision P07-T06 rules 3, 5 and 7)."""

from __future__ import annotations

from typing import Any

import pytest
from graph_helpers import node
from handler_helpers import FakeLines, handler_ctx, spoken, unavailable
from service_helpers import json_log_stream, log_events

from va_engine.handlers import (
    DEFAULT_TEXT_MODE_MESSAGE,
    Channel,
    HandlerResult,
    TransferCallHandler,
    handler_for,
    resolve_transfer_target,
)
from va_engine.models import TransferAction

HANDLER = TransferCallHandler()
TARGET = "+14155550123"


def transfer_node(**data: Any) -> dict[str, Any]:
    return node("xfer", node_type="Transfer Call", **data)


async def test_cold_transfer_after_the_final_line() -> None:
    ctx = handler_ctx(transfer_node(transfer={"number": TARGET, "speech": "Transferring you now."}))

    result = await HANDLER.respond(ctx)

    assert await spoken(result.actions) == [
        ("say", "Transferring you now.", "t3.1", False),
        ("transfer", "cold", TARGET, "agent_number", "t3.1"),
    ]
    assert result.terminal
    assert result.problem is None
    assert not ctx.state.ended


@pytest.mark.parametrize(
    ("data", "variables", "transfer_list", "expected"),
    [
        ({"transferNumber": TARGET}, {}, {}, TARGET),
        ({"transfer": {"number": "  "}, "transferNumber": TARGET}, {}, {}, TARGET),
        ({"transfer": {"number": "{{manager}}"}}, {"manager": TARGET}, {}, TARGET),
        ({"transfer": {"number": "sales"}}, {}, {"sales": TARGET}, TARGET),
        (
            {"transfer": {"number": "{{team}}"}},
            {"team": "billing"},
            {"billing": " +442071234567 "},
            "+442071234567",
        ),
        ({"transfer": {"number": "+1 (415) 555-0123"}}, {}, {}, TARGET),
        ({"transfer": {"number": "sip:desk@pbx.example.org"}}, {}, {}, "sip:desk@pbx.example.org"),
        ({"transfer": {"number": "SIPS:desk@pbx.example.org"}}, {}, {}, "SIPS:desk@pbx.example.org"),
    ],
)
def test_target_resolution(
    data: dict[str, Any], variables: dict[str, Any], transfer_list: dict[str, str], expected: str
) -> None:
    ctx = handler_ctx(transfer_node(**data), variables=variables, transfer_list=transfer_list)

    assert resolve_transfer_target(ctx) == expected


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        ({}, "missing"),
        ({"transfer": {"number": "   "}}, "missing"),
        ({"transfer": {"number": "4155550123"}}, "invalid"),
        ({"transfer": {"number": "+0123456789"}}, "invalid"),
        ({"transfer": {"number": "+1415"}}, "invalid"),
        ({"transfer": {"number": "{{nobody}}"}}, "invalid"),
        ({"transfer": {"number": "unknown_label"}}, "invalid"),
        ({"transfer": {"number": "sip:"}}, "invalid"),
    ],
)
async def test_invalid_target_speaks_the_error_phrase_without_transfer(
    data: dict[str, Any], reason: str
) -> None:
    stream = json_log_stream()
    ctx = handler_ctx(transfer_node(**data), transfer_list={"sales": TARGET})

    result = await HANDLER.respond(ctx)

    assert await spoken(result.actions) == [("say", "I'm sorry, I'm having trouble right now.", "t3.1", None)]
    assert result.problem == "transfer_target_invalid"
    assert not result.terminal
    events = [line for line in log_events(stream) if line.get("event") == "engine.transfer_target_invalid"]
    assert [(event["node_id"], event["reason"]) for event in events] == [("xfer", reason)]
    assert "4155550123" not in stream.getvalue()


async def test_blank_error_phrase_says_nothing() -> None:
    ctx = handler_ctx(transfer_node(), global_config={"errorPhrase": " "})

    result = await HANDLER.respond(ctx)

    assert result == HandlerResult((), problem="transfer_target_invalid")


async def test_the_number_is_never_logged() -> None:
    stream = json_log_stream()
    ctx = handler_ctx(
        transfer_node(transfer={"number": TARGET, "speechMode": "prompt", "speechPrompt": "Say it."}),
        lines=FakeLines(reply=unavailable()),
    )

    await HANDLER.respond(ctx)

    assert TARGET not in stream.getvalue()


async def test_prompt_speech_mode_composes_the_line() -> None:
    lines = FakeLines(reply="Connecting you to {{x}} now.")
    ctx = handler_ctx(
        transfer_node(
            transfer={
                "number": TARGET,
                "speech": "One moment.",
                "speechMode": "prompt",
                "speechPrompt": "Tell {{first_name}} you are connecting them.",
            }
        ),
        variables={"first_name": "Ann"},
        lines=lines,
    )

    result = await HANDLER.respond(ctx)

    assert lines.instructions == ["Tell Ann you are connecting them."]
    assert (await spoken(result.actions))[0] == ("say", "Connecting you to {{x}} now.", "t3.1", False)


async def test_prompt_speech_mode_falls_back_to_speech() -> None:
    ctx = handler_ctx(
        transfer_node(
            transfer={
                "number": TARGET,
                "speech": "One moment.",
                "speechMode": "prompt",
                "speechPrompt": "Hi.",
            }
        ),
        lines=FakeLines(reply=unavailable()),
    )

    result = await HANDLER.respond(ctx)

    assert await spoken(result.actions) == [
        ("say", "One moment.", "t3.1", False),
        ("transfer", "cold", TARGET, "agent_number", "t3.1"),
    ]


async def test_without_any_line_the_transfer_is_due_at_once() -> None:
    ctx = handler_ctx(transfer_node(transferNumber=TARGET))

    result = await HANDLER.respond(ctx)

    assert await spoken(result.actions) == [("transfer", "cold", TARGET, "agent_number", None)]


async def test_caller_id_mode_and_warm_settings_pass_through() -> None:
    warm = {"briefingPrompt": "Brief the agent.", "holdAudio": "silence", "targetTimeoutSecs": 45}
    ctx = handler_ctx(
        transfer_node(
            transfer={"mode": "warm", "number": TARGET, "callerIdMode": "original_caller", "warm": warm}
        )
    )

    result = await HANDLER.respond(ctx)

    (action,) = result.actions
    assert isinstance(action, TransferAction)
    assert (action.mode, action.caller_id_mode) == ("warm", "original_caller")
    assert action.warm is not None
    assert (action.warm.briefing_prompt, action.warm.hold_audio, action.warm.target_timeout_secs) == (
        "Brief the agent.",
        "silence",
        45,
    )


async def test_cold_mode_drops_warm_settings() -> None:
    ctx = handler_ctx(transfer_node(transfer={"number": TARGET, "warm": {"holdAudio": "silence"}}))

    (action,) = (await HANDLER.respond(ctx)).actions

    assert isinstance(action, TransferAction)
    assert action.warm is None


@pytest.mark.parametrize(
    ("transfer", "message"),
    [
        ({"number": TARGET}, DEFAULT_TEXT_MODE_MESSAGE),
        (None, DEFAULT_TEXT_MODE_MESSAGE),
        ({"number": TARGET, "textModeMessage": "  "}, DEFAULT_TEXT_MODE_MESSAGE),
        ({"number": TARGET, "textModeMessage": "{{nothing}}"}, DEFAULT_TEXT_MODE_MESSAGE),
        ({"number": TARGET, "textModeMessage": 7}, DEFAULT_TEXT_MODE_MESSAGE),
        ({"textModeMessage": "{{first_name}}, Sam will text you."}, "Ann, Sam will text you."),
    ],
)
async def test_sms_sends_the_text_mode_message_then_hands_over(
    transfer: dict[str, Any] | None, message: str
) -> None:
    data = {"transfer": transfer} if transfer is not None else {}
    ctx = handler_ctx(transfer_node(**data), channel="sms", variables={"first_name": "Ann"})

    result = await HANDLER.respond(ctx)

    assert await spoken(result.actions) == [("say", message, "t3.1", False), ("takeover", "t3.1")]
    assert result.terminal


@pytest.mark.parametrize("channel", ["widget_chat", "widget_voice"])
async def test_widget_channels_hand_over_at_once(channel: Channel) -> None:
    ctx = handler_ctx(transfer_node(transfer={"number": TARGET, "speech": "Bye."}), channel=channel)

    result = await HANDLER.respond(ctx)

    assert await spoken(result.actions) == [("takeover", None)]


async def test_on_enter_does_nothing_and_never_waits() -> None:
    ctx = handler_ctx(transfer_node(transferNumber=TARGET))

    assert await HANDLER.on_enter(ctx) == HandlerResult()
    assert not HANDLER.waits_for_user(ctx.node)
    assert isinstance(handler_for(ctx.node), TransferCallHandler)
